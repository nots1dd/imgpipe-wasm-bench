//! edgegun — minimal HTTP load generator for edgeserve.
//!
//! C connections × R keep-alive requests each; measures per-request latency
//! client-side (the number a user of the edge service would actually feel)
//! and prints one JSON summary line.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

fn argval(args: &[String], key: &str) -> Option<String> {
    args.iter()
        .position(|a| a == key)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((q / 100.0) * (sorted.len() as f64 - 1.0)).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

/// Read one response: headers to \r\n\r\n, then Content-Length bytes.
fn read_response(s: &mut TcpStream, buf: &mut Vec<u8>) -> std::io::Result<usize> {
    buf.clear();
    let mut chunk = [0u8; 64 * 1024];
    let hdr_end = loop {
        if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            break pos + 4;
        }
        let n = s.read(&mut chunk)?;
        if n == 0 {
            return Err(std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "eof"));
        }
        buf.extend_from_slice(&chunk[..n]);
    };
    let head = String::from_utf8_lossy(&buf[..hdr_end]);
    let mut clen: usize = 0;
    for l in head.lines() {
        let low = l.to_ascii_lowercase();
        if let Some(v) = low.strip_prefix("content-length: ") {
            clen = v.trim().parse().unwrap_or(0);
        }
    }
    while buf.len() < hdr_end + clen {
        let n = s.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    Ok(clen)
}

fn worker(host: &str, port: u16, path: &str, reqs: usize, close: bool) -> (Vec<f64>, usize, usize) {
    let mut lat = Vec::with_capacity(reqs);
    let mut errors = 0usize;
    let mut bytes = 0usize;
    let addr = format!("{host}:{port}");
    let conn_hdr = if close { "Connection: close\r\n" } else { "" };
    let req = format!("GET {path} HTTP/1.1\r\nHost: {addr}\r\n{conn_hdr}\r\n");

    if close {
        // new TCP connection per request (also pays connect cost — same for all targets)
        for _ in 0..reqs {
            let t0 = Instant::now();
            let r = TcpStream::connect(&addr).and_then(|mut s| {
                let _ = s.set_nodelay(true);
                let _ = s.set_read_timeout(Some(Duration::from_secs(30)));
                let _ = s.set_write_timeout(Some(Duration::from_secs(30)));
                let mut buf = Vec::with_capacity(128 * 1024);
                s.write_all(req.as_bytes())
                    .and_then(|_| read_response(&mut s, &mut buf))
                    .map(|n| bytes += n)
            });
            match r {
                Ok(_) => lat.push(t0.elapsed().as_secs_f64() * 1000.0),
                Err(_) => errors += 1,
            }
        }
        return (lat, errors, bytes);
    }

    let mut s = match TcpStream::connect(&addr) {
        Ok(s) => s,
        Err(_) => return (lat, reqs, 0), // whole connection failed
    };
    let _ = s.set_nodelay(true);
    let _ = s.set_read_timeout(Some(Duration::from_secs(30)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(30)));
    let mut buf = Vec::with_capacity(128 * 1024);
    for _ in 0..reqs {
        let t0 = Instant::now();
        let ok = s
            .write_all(req.as_bytes())
            .and_then(|_| read_response(&mut s, &mut buf))
            .map(|n| bytes += n)
            .is_ok();
        if ok {
            lat.push(t0.elapsed().as_secs_f64() * 1000.0);
        } else {
            errors += 1;
            break; // connection dead; don't mask it by reconnecting
        }
    }
    (lat, errors, bytes)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let host = argval(&args, "--host").unwrap_or_else(|| "127.0.0.1".into());
    let port: u16 = argval(&args, "--port")
        .and_then(|v| v.parse().ok())
        .unwrap_or(8080);
    let path = argval(&args, "--path").unwrap_or_else(|| "/echo?n=1024".into());
    let conns: usize = argval(&args, "--conns")
        .and_then(|v| v.parse().ok())
        .unwrap_or(1);
    let reqs: usize = argval(&args, "--reqs")
        .and_then(|v| v.parse().ok())
        .unwrap_or(1000);
    let close = args.iter().any(|a| a == "--close");

    let t0 = Instant::now();
    let handles: Vec<_> = (0..conns)
        .map(|_| {
            let (h, pth) = (host.clone(), path.clone());
            std::thread::spawn(move || worker(&h, port, &pth, reqs, close))
        })
        .collect();
    let mut lat = Vec::new();
    let mut errors = 0usize;
    let mut bytes = 0usize;
    for h in handles {
        if let Ok((l, e, b)) = h.join() {
            lat.extend(l);
            errors += e;
            bytes += b;
        }
    }
    let wall = t0.elapsed().as_secs_f64();
    lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let done = lat.len();
    println!(
        "{{\"conns\":{},\"reqs_per_conn\":{},\"requests\":{},\"errors\":{},\"wall_ms\":{:.1},\"req_per_s\":{:.1},\"p50_ms\":{:.4},\"p95_ms\":{:.4},\"p99_ms\":{:.4},\"bytes\":{}}}",
        conns,
        reqs,
        done,
        errors,
        wall * 1000.0,
        done as f64 / wall.max(1e-9),
        percentile(&lat, 50.0),
        percentile(&lat, 95.0),
        percentile(&lat, 99.0),
        bytes
    );
}
