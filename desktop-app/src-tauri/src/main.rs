#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{TcpListener, TcpStream};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

struct Sidecar(Mutex<Option<CommandChild>>);

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
            let port = free_port();

            let (_rx, child) = app
                .shell()
                .sidecar("wfgps")?
                .args(["desktop", "--host", "127.0.0.1", "--port", &port.to_string()])
                .spawn()?;
            app.state::<Sidecar>().0.lock().unwrap().replace(child);

            wait_ready(port, Duration::from_secs(20));

            let inject = format!("window.__OOLU_API__ = 'http://127.0.0.1:{port}';");
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
                if let Some(child) = app.state::<Sidecar>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
