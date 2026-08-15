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
use url::Url;

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

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateCheck {
    current_version: String,
    status: String,
    message: String,
    update: Option<UpdateInfo>,
    can_install: bool,
}

async fn available_update(app: &AppHandle) -> Result<Option<Update>, String> {
    app.updater()
        .map_err(|error| error.to_string())?
        .check()
        .await
        .map_err(|error| error.to_string())
}

fn app_bundle_path() -> Result<PathBuf, String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("Synchri could not locate its application bundle: {error}"))?;
    let macos_dir = executable
        .parent()
        .ok_or("Synchri could not determine its application bundle path")?;
    let contents_dir = macos_dir
        .parent()
        .ok_or("Synchri could not determine its application bundle path")?;
    contents_dir
        .parent()
        .map(PathBuf::from)
        .ok_or_else(|| "Synchri could not determine its application bundle path".to_string())
}

fn can_install_update_in_place(bundle: &std::path::Path) -> bool {
    // A downloaded app is commonly launched through App Translocation. Its
    // path is a temporary, macOS-owned mount; replacing it makes the original
    // download appear to disappear. Only update a normal Applications install.
    if bundle
        .components()
        .any(|part| part.as_os_str() == "AppTranslocation")
    {
        return false;
    }
    if bundle.starts_with("/Applications") {
        return true;
    }
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .map(|home| bundle.starts_with(home.join("Applications")))
        .unwrap_or(false)
}

fn update_install_location_message() -> String {
    "Move Synchri.app to Applications before installing updates. Synchri will never replace an app opened directly from a download.".to_string()
}

#[tauri::command]
async fn check_for_update(app: AppHandle) -> UpdateCheck {
    let current_version = app.package_info().version.to_string();
    let can_install = app_bundle_path()
        .map(|bundle| can_install_update_in_place(&bundle))
        .unwrap_or(false);
    // Do this before a network request. A downloaded or translocated app is
    // never a safe update target, and the user needs that installation answer
    // even while offline.
    if !can_install {
        return UpdateCheck {
            current_version,
            status: "move_to_applications".into(),
            message: update_install_location_message(),
            update: None,
            can_install,
        };
    }
    match available_update(&app).await {
        Ok(Some(update)) => UpdateCheck {
            current_version,
            status: "available".into(),
            message: format!("Synchri {} is ready to install.", update.version),
            update: Some(UpdateInfo {
                version: update.version,
                notes: update.body,
                date: update.date.map(|date| date.to_string()),
            }),
            can_install,
        },
        Ok(None) => UpdateCheck {
            current_version,
            status: "current".into(),
            message: "Synchri is up to date.".into(),
            update: None,
            can_install,
        },
        Err(error) => UpdateCheck {
            current_version,
            status: "error".into(),
            message: format!("Synchri could not check for updates: {error}"),
            update: None,
            can_install,
        },
    }
}

#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    let bundle = app_bundle_path()?;
    if !can_install_update_in_place(&bundle) {
        return Err(update_install_location_message());
    }
    let Some(update) = available_update(&app).await? else {
        return Ok(());
    };
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| error.to_string())?;
    app.restart();
}

fn github_https_url(value: &str) -> Result<Url, String> {
    let parsed =
        Url::parse(value).map_err(|_| "Synchri received an invalid GitHub address".to_string())?;
    let host = parsed.host_str().unwrap_or_default();
    if parsed.scheme() != "https" || !host.eq_ignore_ascii_case("github.com") {
        return Err("Synchri can only open secure GitHub addresses from this action".into());
    }
    Ok(parsed)
}

#[tauri::command]
fn open_github_url(url: String) -> Result<(), String> {
    let url = github_https_url(&url)?;
    let status = Command::new("open")
        .arg(url.as_str())
        .status()
        .map_err(|error| format!("Synchri could not open GitHub in your browser: {error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err("Synchri could not open GitHub in your browser".into())
    }
}

#[cfg(test)]
mod tests {
    use super::{can_install_update_in_place, github_https_url};
    use std::path::Path;

    #[test]
    fn updater_refuses_translocated_and_downloaded_bundles() {
        assert!(!can_install_update_in_place(Path::new(
            "/private/var/folders/x/AppTranslocation/id/d/Synchri.app"
        )));
        assert!(!can_install_update_in_place(Path::new(
            "/Users/person/Downloads/Synchri.app"
        )));
    }

    #[test]
    fn updater_accepts_applications_bundles() {
        assert!(can_install_update_in_place(Path::new(
            "/Applications/Synchri.app"
        )));
    }

    #[test]
    fn external_browser_only_receives_secure_github_urls() {
        assert!(github_https_url("https://github.com/login/device").is_ok());
        assert!(github_https_url("http://github.com/login/device").is_err());
        assert!(github_https_url("https://example.com").is_err());
    }
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
    *state
        .child
        .lock()
        .map_err(|_| "Synchri's local engine lock was poisoned")? = Some(child);

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
        .invoke_handler(tauri::generate_handler![
            check_for_update,
            install_update,
            open_github_url
        ])
        // A hidden local placeholder keeps macOS's application lifecycle alive
        // while the engine starts. It is navigated to the fresh, authenticated
        // loopback URL before the window is ever revealed.
        .build(tauri::generate_context!())
        .expect("error while building Synchri")
        .run(move |app_handle, event| match event {
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
        });
}
