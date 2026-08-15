//! Reproduce Tauri's macOS updater extraction exactly for release verification.
//!
//! macOS's BSD tar understands AppleDouble sidecars and may hide them while
//! listing or extracting an archive. Tauri uses Rust's `tar` crate instead;
//! any sidecar in the transport becomes a literal `._*` file inside the app
//! and invalidates its outer code signature. This helper deliberately mirrors
//! `tauri-plugin-updater`'s extraction loop so CI validates the real consumer.

use flate2::read::GzDecoder;
use std::{env, fs::File, io, path::PathBuf};

fn main() -> io::Result<()> {
    let mut arguments = env::args_os();
    arguments.next();
    let archive_path = arguments.next().expect("expected updater archive path");
    let destination = PathBuf::from(arguments.next().expect("expected extraction directory"));

    let decoder = GzDecoder::new(File::open(archive_path)?);
    let mut archive = tar::Archive::new(decoder);

    for entry in archive.entries()? {
        let mut entry = entry?;
        // Keep this aligned with tauri-plugin-updater's macOS install path:
        // it drops the enclosing `Synchri.app` directory before unpacking.
        let relative_path: PathBuf = entry.path()?.iter().skip(1).collect();
        let extraction_path = destination.join(relative_path);
        if let Some(parent) = extraction_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        entry.unpack(extraction_path)?;
    }

    Ok(())
}
