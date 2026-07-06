#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{TcpListener, TcpStream};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

struct Sidecar(Mutex<Option<CommandChild>>);

// The online host is baked in at build (setup) time, never edited by the user:
// compile with OOLU_SERVER_URL=https://your-host to ship a client that signs
// into that host. Left unset (the default today, while the domain is pending)
// the app runs the local loopback engine as a sidecar — the offline/solo mode.
const SERVER_URL: Option<&str> = option_env!("OOLU_SERVER_URL");

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("bind loopback")
        .local_addr()
        .unwrap()
        .port()
}

fn wait_ready(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            // A non-empty build-time server URL selects remote mode.
            let remote = SERVER_URL.filter(|url| !url.is_empty());
            let api_base = match remote {
                Some(url) => {
                    // Remote: talk to the online host directly — no sidecar,
                    // the front-end shows a sign-in screen (see api.ts).
                    url.to_string()
                }
                None => {
                    // Local: spawn the loopback engine and point the app at it.
                    let port = free_port();
                    let (_rx, child) = app
                        .shell()
                        .sidecar("oolu")?
                        .args(["desktop", "--host", "127.0.0.1", "--port", &port.to_string()])
                        .spawn()?;
                    app.state::<Sidecar>().0.lock().unwrap().replace(child);
                    wait_ready(port, Duration::from_secs(20));
                    format!("http://127.0.0.1:{port}")
                }
            };

            let is_remote = remote.is_some();
            let inject = format!(
                "window.__OOLU_API__ = '{api_base}'; window.__OOLU_REMOTE__ = {is_remote};"
            );
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("OoLu")
                .inner_size(900.0, 720.0)
                .min_inner_size(640.0, 480.0)
                .initialization_script(&inject)
                .build()?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("run OoLu")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                // Only populated in local mode; a no-op remote-side.
                if let Some(child) = app.state::<Sidecar>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
