//! Native macOS shell for Synchri.
//!
//! The Python sidecar remains the only owner of room state and the local HTTP
//! interface. This process gives it a signed, updater-aware desktop window;
//! it never listens on a network socket or duplicates broker behaviour.

use serde::Serialize;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_updater::{Update, UpdaterExt};

#[derive(Default)]
struct SidecarState {
    child: Mutex<Option<Child>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateInfo {
    version: String,
    notes: Option<String>,
    date: Option<String>,
}

async fn available_update(app: &AppHandle) -> Result<Option<Update>, String> {
    app.updater()
        .map_err(|error| error.to_string())?
        .check()
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn check_for_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    let Some(update) = available_update(&app).await? else {
        return Ok(None);
    };
    Ok(Some(UpdateInfo {
        version: update.version,
        notes: update.body,
        date: update.date.map(|date| date.to_string()),
    }))
}

#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    let Some(update) = available_update(&app).await? else {
        return Ok(());
    };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| error.to_string())?;
    app.restart();
}

fn bundled_engine(_app: &AppHandle) -> Result<PathBuf, String> {
    // This is deliberately based on the executable's own app bundle. The
    // macOS application layout is fixed and makes the engine location explicit.
    let executable = std::env::current_exe()
        .map_err(|error| format!("Synchri could not locate its native shell: {error}"))?;
    let macos_dir = executable
        .parent()
        .ok_or("Synchri could not determine its app bundle path")?;
    Ok(macos_dir
        .join("..")
        .join("Resources")
        .join("engine")
        .join("synchri-core"))
}

fn open_desktop_window(app: AppHandle, url: &str) -> Result<(), String> {
    let url = url
        .parse()
        .map_err(|error| format!("Synchri received an invalid local engine address: {error}"))?;
    let window = app
        .get_webview_window("main")
        .ok_or("Synchri could not create its desktop window")?;
    window
        .navigate(url)
        .map_err(|error| format!("Synchri could not create its desktop window: {error}"))?;
    window
        .show()
        .map_err(|error| format!("Synchri could not show its desktop window: {error}"))?;
    window
        .set_focus()
        .map_err(|error| format!("Synchri could not focus its desktop window: {error}"))?;
    Ok(())
}

fn start_sidecar(app: AppHandle, state: Arc<SidecarState>) -> Result<(), String> {
    let engine = bundled_engine(&app)?;
    if !engine.is_file() {
        return Err(format!(
            "Synchri's bundled engine is missing from {}",
            engine.display()
        ));
    }
    let engine_dir = engine
        .parent()
        .ok_or("Synchri could not determine its engine directory")?;
    let mut child = Command::new(&engine)
        .args(["ui", "--no-open", "--port", "0"])
        .current_dir(engine_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Synchri could not start its local engine: {error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or("Synchri could not read its local engine startup address")?;
    let stderr = child
        .stderr
        .take()
        .ok_or("Synchri could not read its local engine diagnostics")?;
    *state.child.lock().map_err(|_| "Synchri's local engine lock was poisoned")? = Some(child);

    // The server generates a fresh capability token for every launch. Wait
    // for that exact URL instead of guessing a port or ever showing an
    // unauthenticated window. A separate reader also prevents a sidecar error
    // from filling stderr and blocking the engine before it can report itself.
    let window_app = app.clone();
    thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let candidate = line.trim();
            if !candidate.starts_with("http://127.0.0.1:") || !candidate.contains("?token=") {
                continue;
            }
            if let Err(error) = open_desktop_window(window_app, candidate) {
                eprintln!("{error}");
            }
            return;
        }
    });
    thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            eprintln!("Synchri engine: {line}");
        }
    });
    Ok(())
}

fn main() {
    let sidecar = Arc::new(SidecarState::default());
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Arc::clone(&sidecar))
        .invoke_handler(tauri::generate_handler![check_for_update, install_update])
        // A hidden local placeholder keeps macOS's application lifecycle alive
        // while the engine starts. It is navigated to the fresh, authenticated
        // loopback URL before the window is ever revealed.
        .build(tauri::generate_context!())
        .expect("error while building Synchri")
        .run(move |app_handle, event| {
            match event {
                RunEvent::Ready => {
                    if let Err(error) = start_sidecar(app_handle.clone(), Arc::clone(&sidecar)) {
                        eprintln!("{error}");
                        app_handle.exit(1);
                    }
                }
                RunEvent::Exit | RunEvent::ExitRequested { .. } => {
                    if let Ok(mut child) = sidecar.child.lock() {
                        if let Some(mut child) = child.take() {
                            let _ = child.kill();
                        }
                    }
                }
                _ => {}
            }
        });
}
