#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::Duration,
};
use tauri::{webview::WebviewWindowBuilder, AppHandle, Manager, RunEvent, WebviewWindow};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_shell::{process::CommandChild, ShellExt};

const DASHBOARD_URL: &str = "http://127.0.0.1:8765/#overview";
const STATUS_HOST: &str = "127.0.0.1:8765";

enum BackendProcess {
    Development(Child),
    Sidecar(CommandChild),
}

#[derive(Default)]
struct BackendState(Mutex<Option<BackendProcess>>);

fn backend_is_ready() -> bool {
    let address: SocketAddr = match STATUS_HOST.parse() {
        Ok(value) => value,
        Err(_) => return false,
    };
    TcpStream::connect_timeout(&address, Duration::from_millis(500)).is_ok()
}

fn project_root() -> PathBuf {
    std::env::var_os("WEI_DAILY_PROJECT_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
}

fn spawn_development_backend(root: &Path) -> Result<BackendProcess, String> {
    let python = root.join(".venv").join("Scripts").join("python.exe");
    if !python.is_file() {
        return Err(format!("未找到 Python 虚拟环境：{}", python.display()));
    }
    let child = Command::new(python)
        .current_dir(root)
        .args([
            "-m", "wechat_bridge", "run",
            "--adapter", "wechatauto_db",
            "--chat", "文件传输助手",
            "--dashboard",
            "--dashboard-host", "127.0.0.1",
            "--dashboard-port", "8765",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(BackendProcess::Development)
        .map_err(|error| format!("无法启动本地服务：{error}"))?;
    Ok(child)
}

fn spawn_packaged_backend(app: &tauri::App) -> Result<BackendProcess, String> {
    let data_dir = app.path().app_data_dir().map_err(|error| error.to_string())?;
    std::fs::create_dir_all(&data_dir).map_err(|error| error.to_string())?;
    let command = app
        .shell()
        .sidecar("wei-daily-backend")
        .map_err(|error| format!("未找到微语 sidecar：{error}"))?
        .env("WEI_DAILY_DATA_DIR", data_dir);
    let (_events, child) = command.spawn().map_err(|error| format!("sidecar 启动失败：{error}"))?;
    Ok(BackendProcess::Sidecar(child))
}

fn stop_owned_backend(app: &tauri::AppHandle) {
    let state = app.state::<BackendState>();
    let Ok(mut slot) = state.0.lock() else { return; };
    let Some(process) = slot.take() else { return; };
    match process {
        BackendProcess::Development(mut child) => { let _ = child.kill(); let _ = child.wait(); }
        BackendProcess::Sidecar(child) => { let _ = child.kill(); }
    }
}

#[tauri::command]
fn backend_ready() -> bool {
    backend_is_ready()
}

#[tauri::command]
fn open_dashboard(window: WebviewWindow) -> Result<(), String> {
    if !backend_is_ready() {
        return Err("本地服务尚未就绪".into());
    }
    let url: tauri::Url = DASHBOARD_URL
        .parse()
        .map_err(|error| format!("地址无效：{error}"))?;
    window.navigate(url).map_err(|error| error.to_string())
}

fn export_extension(format: &str) -> Result<&'static str, String> {
    match format.trim().to_ascii_lowercase().as_str() {
        "html" => Ok("html"),
        "pdf" => Ok("pdf"),
        _ => Err("仅支持 HTML 或 PDF 导出".into()),
    }
}

fn export_suggested_filename(file_name: &str, extension: &str) -> String {
    let mut name = Path::new(file_name)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.trim().is_empty() && *value != "." && *value != "..")
        .unwrap_or("wechat-intelligence-report")
        .to_string();
    if Path::new(&name).extension().is_none() {
        name.push('.');
        name.push_str(extension);
    }
    name
}

#[tauri::command(rename_all = "camelCase")]
async fn save_export_file(
    app: AppHandle,
    file_name: String,
    format: String,
    bytes: Vec<u8>,
) -> Result<Option<String>, String> {
    if bytes.is_empty() {
        return Err("导出内容为空".into());
    }
    let extension = export_extension(&format)?;
    let suggested_name = export_suggested_filename(&file_name, extension);
    let dialog = app
        .dialog()
        .file()
        .set_title("保存微语")
        .set_file_name(suggested_name);
    let dialog = if extension == "pdf" {
        dialog.add_filter("PDF 文件", &["pdf"])
    } else {
        dialog.add_filter("HTML 文件", &["html"])
    };
    let Some(file_path) = dialog.blocking_save_file() else {
        return Ok(None);
    };
    let selected_path = file_path
        .into_path()
        .map_err(|error| format!("无法读取保存路径：{error}"))?;
    let final_path = if selected_path.extension().is_none() {
        let mut path = selected_path;
        path.set_extension(extension);
        path
    } else {
        selected_path
    };
    std::fs::write(&final_path, &bytes)
        .map_err(|error| format!("写入导出文件失败：{}：{error}", final_path.display()))?;
    Ok(Some(final_path.to_string_lossy().into_owned()))
}

#[cfg(test)]
mod tests {
    use super::{export_extension, export_suggested_filename};

    #[test]
    fn export_formats_are_limited_to_html_and_pdf() {
        assert_eq!(export_extension("HTML"), Ok("html"));
        assert_eq!(export_extension(" pdf "), Ok("pdf"));
        assert!(export_extension("docx").is_err());
    }

    #[test]
    fn suggested_filename_keeps_basename_and_adds_missing_extension() {
        assert_eq!(
            export_suggested_filename("nested/report", "pdf"),
            "report.pdf"
        );
        assert_eq!(
            export_suggested_filename("report.html", "html"),
            "report.html"
        );
        assert_eq!(
            export_suggested_filename("", "html"),
            "wechat-intelligence-report.html"
        );
    }
}

fn navigate_when_ready(app_handle: AppHandle) {
    std::thread::spawn(move || {
        for _ in 0..120 {
            if backend_is_ready() {
                let navigation_handle = app_handle.clone();
                let _ = app_handle.run_on_main_thread(move || {
                    let Some(window) = navigation_handle.get_webview_window("main") else {
                        return;
                    };
                    let Ok(url) = DASHBOARD_URL.parse::<tauri::Url>() else {
                        return;
                    };
                    let _ = window.navigate(url);
                });
                return;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
    });
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![backend_ready, open_dashboard, save_export_file])
        .setup(|app| {
            let window_config = app
                .config()
                .app
                .windows
                .iter()
                .find(|config| config.label == "main")
                .cloned()
                .ok_or_else(|| std::io::Error::other("缺少主窗口配置"))?;
            WebviewWindowBuilder::from_config(app, &window_config)
                .map_err(std::io::Error::other)?
                .on_download(|_, _| true)
                .build()
                .map_err(std::io::Error::other)?;
            if !backend_is_ready() {
                let process = if cfg!(debug_assertions) {
                    spawn_development_backend(&project_root()).map_err(std::io::Error::other)?
                } else {
                    spawn_packaged_backend(app).map_err(std::io::Error::other)?
                };
                let state = app.state::<BackendState>();
                *state
                    .0
                    .lock()
                    .map_err(|_| std::io::Error::other("后端状态锁不可用"))? = Some(process);
            }
            navigate_when_ready(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Weiyu desktop application");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_owned_backend(app_handle);
        }
    });
}
