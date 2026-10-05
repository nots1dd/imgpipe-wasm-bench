//! imgpipe — edge image-pipeline benchmark binary.
//!
//! One codebase, three artifacts:
//!   * x86_64-unknown-linux-musl  -> scratch container image / Firecracker rootfs
//!   * wasm32-wasip1              -> wasmtime / runwasi (containerd shim)
//!
//! Real task: for every JPEG in --in, decode -> resize to a thumbnail
//! (Lanczos3) -> re-encode as JPEG -> write to --out. Ablation modes isolate
//! which stage (and which syscall layer) costs what.

use std::error::Error;
use std::fs;
use std::io::Cursor;
use std::path::Path;
use std::time::Instant;

use image::codecs::jpeg::{JpegDecoder, JpegEncoder};
use image::imageops::FilterType;
use image::{DynamicImage, Rgb, RgbImage};

const VERSION: &str = env!("CARGO_PKG_VERSION");

struct Cfg {
    mode: String,
    input: String,
    out: String,
    width: u32,
    height: u32,
    count: usize,
    thumb: u32,
    quality: u8,
}

fn argval(args: &[String], key: &str) -> Option<String> {
    args.iter()
        .position(|a| a == key)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn parse_args() -> Cfg {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--help" || a == "-h") {
        eprintln!(
            "imgpipe {VERSION}\n\
             modes: gen | noop | full | no-io | decode-only | resize-only | io-only\n\
             flags: --mode M --in DIR --out DIR --size WxH --count N --thumb PX --quality Q"
        );
        std::process::exit(0);
    }
    let size = argval(&args, "--size").unwrap_or_else(|| "640x480".into());
    let mut sp = size.split('x');
    Cfg {
        mode: argval(&args, "--mode").unwrap_or_else(|| "full".into()),
        input: argval(&args, "--in").unwrap_or_else(|| "./dataset".into()),
        out: argval(&args, "--out").unwrap_or_else(|| "./out".into()),
        width: sp.next().and_then(|s| s.parse().ok()).unwrap_or(640),
        height: sp.next().and_then(|s| s.parse().ok()).unwrap_or(480),
        count: argval(&args, "--count")
            .and_then(|s| s.parse().ok())
            .unwrap_or(40),
        thumb: argval(&args, "--thumb")
            .and_then(|s| s.parse().ok())
            .unwrap_or(256),
        quality: argval(&args, "--quality")
            .and_then(|s| s.parse().ok())
            .unwrap_or(80),
    }
}

/// Peak RSS of this process (Linux only; None under WASI).
fn vm_hwm_kb() -> Option<u64> {
    let s = fs::read_to_string("/proc/self/status").ok()?;
    s.lines()
        .find(|l| l.starts_with("VmHWM"))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|v| v.parse().ok())
}

fn list_inputs(dir: &str) -> Result<Vec<String>, Box<dyn Error>> {
    let mut v: Vec<String> = fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.extension()
                .and_then(|e| e.to_str())
                .map(|e| e.eq_ignore_ascii_case("jpg") || e.eq_ignore_ascii_case("jpeg"))
                .unwrap_or(false)
        })
        .filter_map(|p| p.to_str().map(|s| s.to_string()))
        .collect();
    v.sort();
    Ok(v)
}

fn decode(bytes: &[u8]) -> Result<DynamicImage, Box<dyn Error>> {
    let dec = JpegDecoder::new(Cursor::new(bytes))?;
    Ok(DynamicImage::from_decoder(dec)?)
}

fn encode_jpeg(img: &DynamicImage, quality: u8) -> Result<Vec<u8>, Box<dyn Error>> {
    let mut buf: Vec<u8> = Vec::new();
    let enc = JpegEncoder::new_with_quality(&mut buf, quality);
    img.write_with_encoder(enc)?;
    Ok(buf)
}

fn percentile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((q / 100.0) * (sorted.len() as f64 - 1.0)).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

/// Procedural test image: gradients + rectangles + high-frequency noise so the
/// JPEG encoder does a realistic amount of work.
fn gen_image(w: u32, h: u32, seed: u64) -> RgbImage {
    let mut state = seed ^ 0x9E37_79B9_7F4A_7C15;
    let mut next = move || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };
    let mut img = RgbImage::new(w, h);
    for y in 0..h {
        for x in 0..w {
            let n = (next() & 0xff) as u8;
            let r = ((x * 255 / w) as u8).wrapping_add(n >> 3);
            let g = ((y * 255 / h) as u8).wrapping_add(n >> 2);
            let b = (((x + y) * 255 / (w + h)) as u8).wrapping_add(n >> 1);
            img.put_pixel(x, y, Rgb([r, g, b]));
        }
    }
    let mut rng = next;
    for _ in 0..12 {
        let rw = (rng() % (w as u64 / 3 + 2)) as u32;
        let rh = (rng() % (h as u64 / 3 + 2)) as u32;
        let rx = (rng() % (w as u64)) as u32;
        let ry = (rng() % (h as u64)) as u32;
        let px = Rgb([(rng() & 0xff) as u8, (rng() & 0xff) as u8, (rng() & 0xff) as u8]);
        for yy in ry..(ry + rh).min(h) {
            for xx in rx..(rx + rw).min(w) {
                img.put_pixel(xx, yy, px);
            }
        }
    }
    img
}

fn mode_gen(cfg: &Cfg) -> Result<(), Box<dyn Error>> {
    fs::create_dir_all(&cfg.out)?;
    let t0 = Instant::now();
    for i in 0..cfg.count {
        let img = gen_image(cfg.width, cfg.height, i as u64 + 1);
        let path = Path::new(&cfg.out).join(format!("img_{i:04}.jpg"));
        let mut f = fs::File::create(&path)?;
        let enc = JpegEncoder::new_with_quality(&mut f, 85);
        DynamicImage::ImageRgb8(img).write_with_encoder(enc)?;
    }
    let wall = t0.elapsed().as_secs_f64() * 1000.0;
    println!(
        "{{\"mode\":\"gen\",\"n\":{},\"size\":\"{}x{}\",\"wall_ms\":{:.3}}}",
        cfg.count, cfg.width, cfg.height, wall
    );
    Ok(())
}

fn mode_bench(cfg: &Cfg) -> Result<(), Box<dyn Error>> {
    if cfg.mode == "noop" {
        // Pure runtime/module initialisation: used for cold-start measurement.
        println!("{{\"mode\":\"noop\",\"wall_ms\":0}}");
        return Ok(());
    }

    fs::create_dir_all(&cfg.out)?;
    let files = list_inputs(&cfg.input)?;
    if files.is_empty() {
        return Err(format!("no JPEGs found in {}", cfg.input).into());
    }

    // no-io pre-reads inputs outside the timed region.
    let preloaded: Vec<Vec<u8>> = if cfg.mode == "no-io" {
        files.iter().map(fs::read).collect::<Result<_, _>>()?
    } else {
        Vec::new()
    };

    let mut lat: Vec<f64> = Vec::with_capacity(files.len());
    let mut bytes_in: u64 = 0;
    let mut bytes_out: u64 = 0;
    let wall0 = Instant::now();

    for (i, path) in files.iter().enumerate() {
        let t0 = Instant::now();
        match cfg.mode.as_str() {
            "full" => {
                let bytes = fs::read(path)?;
                bytes_in += bytes.len() as u64;
                let img = decode(&bytes)?;
                let thumb = img.resize_exact(cfg.thumb, cfg.thumb, FilterType::Lanczos3);
                let out = encode_jpeg(&thumb, cfg.quality)?;
                bytes_out += out.len() as u64;
                fs::write(Path::new(&cfg.out).join(format!("thumb_{i:04}.jpg")), &out)?;
            }
            "no-io" => {
                let bytes = &preloaded[i];
                bytes_in += bytes.len() as u64;
                let img = decode(bytes)?;
                let thumb = img.resize_exact(cfg.thumb, cfg.thumb, FilterType::Lanczos3);
                let out = encode_jpeg(&thumb, cfg.quality)?;
                bytes_out += out.len() as u64;
                std::hint::black_box(out);
            }
            "decode-only" => {
                let bytes = fs::read(path)?;
                bytes_in += bytes.len() as u64;
                let img = decode(&bytes)?;
                std::hint::black_box(img);
            }
            "resize-only" => {
                let bytes = fs::read(path)?;
                bytes_in += bytes.len() as u64;
                let img = decode(&bytes)?;
                let thumb = img.resize_exact(cfg.thumb, cfg.thumb, FilterType::Lanczos3);
                std::hint::black_box(thumb);
            }
            "io-only" => {
                let bytes = fs::read(path)?;
                bytes_in += bytes.len() as u64;
                bytes_out += bytes.len() as u64;
                fs::write(Path::new(&cfg.out).join(format!("copy_{i:04}.jpg")), &bytes)?;
            }
            other => return Err(format!("unknown mode: {other}").into()),
        }
        lat.push(t0.elapsed().as_secs_f64() * 1000.0);
    }

    let wall_ms = wall0.elapsed().as_secs_f64() * 1000.0;
    let mut sorted = lat.clone();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mean = lat.iter().sum::<f64>() / lat.len() as f64;
    let n = files.len();
    let hwm = vm_hwm_kb()
        .map(|v| format!("{v}"))
        .unwrap_or_else(|| "null".into());
    println!(
        "{{\"mode\":\"{}\",\"n\":{},\"wall_ms\":{:.3},\"mean_ms\":{:.3},\"p50_ms\":{:.3},\"p95_ms\":{:.3},\"p99_ms\":{:.3},\"imgs_per_s\":{:.3},\"bytes_in\":{},\"bytes_out\":{},\"vm_hwm_kb\":{},\"imgpipe_version\":\"{}\"}}",
        cfg.mode,
        n,
        wall_ms,
        mean,
        percentile(&sorted, 50.0),
        percentile(&sorted, 95.0),
        percentile(&sorted, 99.0),
        n as f64 / (wall_ms / 1000.0),
        bytes_in,
        bytes_out,
        hwm,
        VERSION
    );
    Ok(())
}

fn main() -> Result<(), Box<dyn Error>> {
    let cfg = parse_args();
    if cfg.mode == "gen" {
        mode_gen(&cfg)
    } else {
        mode_bench(&cfg)
    }
}
