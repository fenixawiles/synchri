fn main() {
    // Synchri deliberately renders its interface from an authenticated,
    // loopback-only server rather than from a bundled HTML asset.  Declare the
    // exact native commands that interface may use so Tauri can enforce them
    // for that remote origin instead of denying every invoke by default.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "check_for_update",
            "install_update",
            "open_github_url",
        ]),
    ))
    .expect("failed to build Synchri's Tauri capability manifest");
}
