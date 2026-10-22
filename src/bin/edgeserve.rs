//! edgeserve — edge HTTP thumbnail service.
//!
//! The network-oriented counterpart to imgpipe: an edge node serving resized
//! previews over HTTP (the Cloudflare-Images / Lambda@Edge pattern). Every
//! request exercises accept + read + write over TCP — the syscall-dense,
//! connection-heavy pattern Review 1 predicted Wasm would struggle with.
//!
//! Endpoints (ablation):
//!   GET /echo?n=K    -> K bytes of payload, zero compute (pure network path)
//!   GET /thumb?id=N  -> decode + Lanczos3 resize + re-encode image N (net + compute)
//!
//! One source, four artifacts: gnu (native/glibc container), musl (scratch
//! container / Firecracker rootfs), wasm32-wasip2 (wasmtime / runwasi).
//! On Wasm there are no OS threads, so connections are served serially —
//! the thread-spawn failure is the portability finding, not a bug.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::time::Instant;

use image::codecs::jpeg::{JpegDecoder, JpegEncoder};
use image::imageops::FilterType;
use image::{DynamicImage, ImageEncoder};

const THUMB: u32 = 256;
const JPEG_Q: u8 = 80;

fn argval(args: &[String], key: &str) -> Option<String> {
    args.iter()
        .position(|a| a == key)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn load_images(dir: &str) -> Vec<Vec<u8>> {
    let mut v: Vec<Vec<u8>> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(dir) {
        let mut paths: Vec<_> = rd
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("jpg"))
            .collect();
        paths.sort();
        for p in paths {
            if let Ok(b) = std::fs::read(&p) {
                v.push(b);
            }
        }
    }
    v
}

fn thumbnail(jpeg: &[u8]) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let dec = JpegDecoder::new(std::io::Cursor::new(jpeg))?;
    let img = DynamicImage::from_decoder(dec)?;
    let thumb = img.resize_exact(THUMB, THUMB, FilterType::Lanczos3);
    let mut out = Vec::new();
    JpegEncoder::new_with_quality(&mut out, JPEG_Q).write_image(
        thumb.to_rgb8().as_raw(),
        THUMB,
        THUMB,
        image::ExtendedColorType::Rgb8,
    )?;
    Ok(out)
}

fn respond(s: &mut TcpStream, body: &[u8]) -> std::io::Result<()> {
    // Single write: wasi:sockets has no TCP_NODELAY knob, so a header-then-body
    // two-write pattern would stall ~40 ms on Nagle/delayed-ACK for small
    // responses. One buffer costs nothing on any target.
    let mut msg = Vec::with_capacity(body.len() + 128);
    msg.extend_from_slice(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .as_bytes(),
    );
    msg.extend_from_slice(body);
    s.write_all(&msg)
}

/// Minimal HTTP/1.1 request handling: read headers, route, respond, honour
/// keep-alive. Returns when the client closes or errs.
fn handle_conn(mut s: TcpStream, images: Arc<Vec<Vec<u8>>>) {
    let _ = s.set_nodelay(true);
    let mut buf = vec![0u8; 64 * 1024];
    let mut used = 0usize;
    loop {
        // fill until we have a full header block
        let hdr_end = loop {
            if let Some(pos) = buf[..used].windows(4).position(|w| w == b"\r\n\r\n") {
                break pos + 4;
            }
            match s.read(&mut buf[used..]) {
                Ok(0) | Err(_) => return,
                Ok(n) => used += n,
            }
        };
        let req = String::from_utf8_lossy(&buf[..hdr_end]);
        let path = req
            .lines()
            .next()
            .and_then(|l| l.split_whitespace().nth(1))
            .unwrap_or("/");
        let close = req.to_ascii_lowercase().contains("connection: close");

        let body = if let Some(q) = path.strip_prefix("/echo") {
            let n: usize = q
                .split("n=")
                .nth(1)
                .and_then(|v| v.split('&').next())
                .and_then(|v| v.parse().ok())
                .unwrap_or(1024);
            vec![b'x'; n.min(4 * 1024 * 1024)]
        } else if let Some(q) = path.strip_prefix("/thumb") {
            if images.is_empty() {
                vec![]
            } else {
                let id: usize = q
                    .split("id=")
                    .nth(1)
                    .and_then(|v| v.split('&').next())
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(0);
                match thumbnail(&images[id % images.len()]) {
                    Ok(b) => b,
                    Err(_) => vec![],
                }
            }
        } else {
            b"edgeserve\n".to_vec()
        };

        if respond(&mut s, &body).is_err() {
            return;
        }
        if close {
            return;
        }
        // shift any pipelined remainder to the front
        let rest = used - hdr_end;
        buf.copy_within(hdr_end..used, 0);
        used = rest;
    }
}

fn vm_hwm_kb() -> Option<u64> {
    let s = std::fs::read_to_string("/proc/self/status").ok()?;
    s.lines()
        .find(|l| l.starts_with("VmHWM"))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|v| v.parse().ok())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let port: u16 = argval(&args, "--port")
        .and_then(|v| v.parse().ok())
        .unwrap_or(8080);
    let data = argval(&args, "--data").unwrap_or_else(|| "./dataset/d640".into());

    let t0 = Instant::now();
    let images = Arc::new(load_images(&data));
    let listener = match TcpListener::bind(("0.0.0.0", port)) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("bind failed: {e}");
            std::process::exit(1);
        }
    };
    // readiness line for the harness: startup includes dataset preload
    let hwm = vm_hwm_kb()
        .map(|v| v.to_string())
        .unwrap_or_else(|| "null".into());
    println!(
        "{{\"ready\":true,\"port\":{},\"images\":{},\"startup_ms\":{:.3},\"vm_hwm_kb\":{}}}",
        port,
        images.len(),
        t0.elapsed().as_secs_f64() * 1000.0,
        hwm
    );
    let _ = std::io::stdout().flush();

    // On targets without OS threads (wasm32-wasip2), spawning fails, so we
    // probe once and serve serially — the spawn failure itself is data.
    let threaded = std::thread::Builder::new().spawn(|| {}).is_ok();
    eprintln!("threading: {}", if threaded { "yes" } else { "no (serial)" });

    for conn in listener.incoming().flatten() {
        let imgs = Arc::clone(&images);
        if threaded {
            std::thread::spawn(move || handle_conn(conn, imgs));
        } else {
            handle_conn(conn, imgs);
        }
    }
}
