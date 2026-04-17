use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const DEFAULT_MODEL: &str = "MiniMax-M2.7";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ConfigForm {
    qq_app_id: String,
    qq_app_secret: String,
    qq_open_id: String,
    minimax_api_key: String,
    model: String,
    msg_format: String,
    qq_enabled: bool,
    autostart_enabled: bool,
    access_mode: String,
    custom_roots: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct LoadedConfig {
    source_path: String,
    local_exists: bool,
    project_root: String,
    system_roots: Vec<String>,
    form: ConfigForm,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct CommandResult {
    ok: bool,
    summary: String,
    stdout: String,
    stderr: String,
    data: Option<Value>,
}

struct ProcessOutput {
    success: bool,
    code: Option<i32>,
    stdout: String,
    stderr: String,
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            load_config,
            save_config,
            check_environment,
            get_status,
            start_stack,
            stop_gateway,
            create_diagnostic_bundle
        ])
        .run(tauri::generate_context!())
        .expect("failed to run phase1 desktop configurator");
}

#[tauri::command]
fn load_config() -> Result<LoadedConfig, String> {
    let project_root = resolve_project_root()?;
    let (raw, source_path, local_exists) = load_source_json(&project_root)?;
    let system_roots = detect_local_drive_roots();
    let mut custom_roots = extract_custom_roots(&raw, &project_root);
    let restrict_to_workspace = get_bool(&raw, &["tools", "restrictToWorkspace"], true);
    let allow_from = get_resolved_array(&raw, &["channels", "qq", "allowFrom"], &project_root);
    let access_mode = if restrict_to_workspace && custom_roots.is_empty() {
        "workspace_only".to_string()
    } else if matches_full_computer(&custom_roots, &system_roots) {
        custom_roots.clear();
        "full_computer".to_string()
    } else {
        "custom_roots".to_string()
    };

    Ok(LoadedConfig {
        source_path: source_path.display().to_string(),
        local_exists,
        project_root: project_root.display().to_string(),
        system_roots,
        form: ConfigForm {
            qq_app_id: get_resolved_string(&raw, &["channels", "qq", "appId"], &project_root),
            qq_app_secret: get_resolved_string(&raw, &["channels", "qq", "secret"], &project_root),
            qq_open_id: allow_from.first().cloned().unwrap_or_default(),
            minimax_api_key: get_resolved_string(&raw, &["providers", "minimax", "apiKey"], &project_root),
            model: {
                let value = get_resolved_string(&raw, &["agents", "defaults", "model"], &project_root);
                if value.is_empty() {
                    DEFAULT_MODEL.to_string()
                } else {
                    value
                }
            },
            msg_format: {
                let value = get_resolved_string(&raw, &["channels", "qq", "msgFormat"], &project_root);
                if value.is_empty() {
                    "plain".to_string()
                } else {
                    value
                }
            },
            qq_enabled: get_bool(&raw, &["channels", "qq", "enabled"], true),
            autostart_enabled: get_bool(&raw, &["phase1", "autostart", "enabled"], true),
            access_mode,
            custom_roots,
        },
    })
}

#[tauri::command]
fn save_config(form: ConfigForm) -> Result<LoadedConfig, String> {
    let project_root = resolve_project_root()?;
    let config_dir = project_root.join("config");
    let local_path = config_dir.join("nanobot.local.json");
    let example_path = config_dir.join("nanobot.example.json");
    let mut raw = if local_path.exists() {
        read_json_file(&local_path)?
    } else {
        read_json_file(&example_path)?
    };

    let qq_open_id = form.qq_open_id.trim().to_string();
    let access_mode = match form.access_mode.as_str() {
        "custom_roots" => "custom_roots",
        "full_computer" => "full_computer",
        _ => "workspace_only",
    };
    let system_roots = detect_local_drive_roots();
    let custom_roots = dedupe_preserve(form.custom_roots);
    let default_project_id =
        get_existing_or_default(&raw, &["phase1", "project", "defaultId"], "phase1-remote-dev");
    let autostart_task_name =
        get_existing_or_default(&raw, &["phase1", "autostart", "taskName"], "Phase1AutoStart");

    set_bool(&mut raw, &["channels", "qq", "enabled"], form.qq_enabled);
    set_string(
        &mut raw,
        &["channels", "qq", "appId"],
        form.qq_app_id.trim().to_string(),
    );
    set_string(
        &mut raw,
        &["channels", "qq", "secret"],
        form.qq_app_secret.trim().to_string(),
    );
    set_string_array(
        &mut raw,
        &["channels", "qq", "allowFrom"],
        if qq_open_id.is_empty() {
            Vec::new()
        } else {
            vec![qq_open_id]
        },
    );
    set_string(
        &mut raw,
        &["channels", "qq", "msgFormat"],
        if form.msg_format.trim().is_empty() {
            "plain".to_string()
        } else {
            form.msg_format.trim().to_string()
        },
    );
    set_string(
        &mut raw,
        &["channels", "qq", "mediaDir"],
        r"${PROJECT_ROOT}\runtime\media\qq".to_string(),
    );

    set_string(
        &mut raw,
        &["agents", "defaults", "workspace"],
        "${PROJECT_ROOT}".to_string(),
    );
    set_string(
        &mut raw,
        &["agents", "defaults", "provider"],
        "minimax".to_string(),
    );
    set_string(
        &mut raw,
        &["agents", "defaults", "model"],
        if form.model.trim().is_empty() {
            DEFAULT_MODEL.to_string()
        } else {
            form.model.trim().to_string()
        },
    );

    set_string(
        &mut raw,
        &["providers", "minimax", "apiKey"],
        form.minimax_api_key.trim().to_string(),
    );

    set_bool(
        &mut raw,
        &["tools", "restrictToWorkspace"],
        access_mode == "workspace_only",
    );

    set_string(
        &mut raw,
        &["phase1", "project", "defaultRoot"],
        "${PROJECT_ROOT}".to_string(),
    );
    set_string(
        &mut raw,
        &["phase1", "project", "defaultId"],
        default_project_id,
    );
    set_bool(&mut raw, &["phase1", "autostart", "enabled"], form.autostart_enabled);
    set_string(
        &mut raw,
        &["phase1", "autostart", "taskName"],
        autostart_task_name,
    );
    set_bool(&mut raw, &["phase1", "computerSearch", "enabled"], true);

    let base_project_roots = vec![
        "${PROJECT_ROOT}".to_string(),
        r"${PROJECT_ROOT}\workspace".to_string(),
    ];
    let base_attachment_roots = vec![r"${PROJECT_ROOT}\runtime\media".to_string()];
    let full_computer_roots = if access_mode == "full_computer" {
        system_roots.clone()
    } else {
        Vec::new()
    };

    let project_roots = if access_mode == "full_computer" {
        merge_roots(&base_project_roots, &full_computer_roots)
    } else if access_mode == "custom_roots" {
        merge_roots(&base_project_roots, &custom_roots)
    } else {
        merge_roots(&base_project_roots, &[])
    };
    let attachment_roots = if access_mode == "full_computer" {
        merge_roots(&base_attachment_roots, &full_computer_roots)
    } else if access_mode == "custom_roots" {
        merge_roots(&base_attachment_roots, &custom_roots)
    } else {
        merge_roots(&base_attachment_roots, &[])
    };
    let artifact_roots = if access_mode == "full_computer" {
        full_computer_roots.clone()
    } else if access_mode == "custom_roots" {
        custom_roots.clone()
    } else {
        Vec::new()
    };

    set_string_array(&mut raw, &["phase1", "project", "allowedRoots"], project_roots);
    set_string_array(
        &mut raw,
        &["phase1", "attachments", "allowedRoots"],
        attachment_roots,
    );
    set_string_array(
        &mut raw,
        &["phase1", "artifacts", "allowedRoots"],
        artifact_roots.clone(),
    );
    set_string_array(
        &mut raw,
        &["phase1", "computerSearch", "allowedRoots"],
        artifact_roots,
    );

    write_json_with_backup(&local_path, &raw)?;
    load_config()
}

#[tauri::command]
fn check_environment() -> Result<CommandResult, String> {
    let project_root = resolve_project_root()?;
    let output = run_powershell_script(&project_root, &["scripts", "Check-Phase1Env.ps1"], &[])?;
    Ok(build_command_result("环境检查", output, None))
}

#[tauri::command]
fn get_status() -> Result<CommandResult, String> {
    let project_root = resolve_project_root()?;
    let output = run_powershell_script(
        &project_root,
        &["scripts", "Get-Phase1Status.ps1"],
        &["-WriteRuntimeHealth"],
    )?;
    let health_path = project_root.join("runtime").join("health.json");
    let data = if health_path.exists() {
        Some(read_json_file(&health_path)?)
    } else {
        None
    };
    Ok(build_command_result("状态刷新", output, data))
}

#[tauri::command]
fn start_stack() -> Result<CommandResult, String> {
    let project_root = resolve_project_root()?;
    let output = run_powershell_script(&project_root, &["scripts", "Start-Phase1Stack.ps1"], &[])?;
    Ok(build_command_result("启动链路", output, None))
}

#[tauri::command]
fn stop_gateway() -> Result<CommandResult, String> {
    let project_root = resolve_project_root()?;
    let output = run_powershell_script(&project_root, &["scripts", "Stop-Phase1Gateway.ps1"], &[])?;
    Ok(build_command_result("停止 Gateway", output, None))
}

#[tauri::command]
fn create_diagnostic_bundle() -> Result<CommandResult, String> {
    let project_root = resolve_project_root()?;
    let output = run_powershell_script(
        &project_root,
        &["scripts", "New-Phase1DiagnosticBundle.ps1"],
        &[],
    )?;
    Ok(build_command_result("生成诊断包", output, None))
}

fn resolve_project_root() -> Result<PathBuf, String> {
    if let Ok(value) = env::var("PHASE1_PROJECT_ROOT") {
        let candidate = PathBuf::from(value);
        if is_phase1_root(&candidate) {
            return Ok(candidate);
        }
    }

    if let Ok(current_dir) = env::current_dir() {
        if let Some(found) = find_project_root(&current_dir) {
            return Ok(found);
        }
    }

    if let Ok(current_exe) = env::current_exe() {
        if let Some(parent) = current_exe.parent() {
            if let Some(found) = find_project_root(parent) {
                return Ok(found);
            }
        }
    }

    Err("找不到 Phase 1 项目根目录。请通过 scripts\\Start-Phase1Configurator.ps1 启动配置器。".to_string())
}

fn find_project_root(start: &Path) -> Option<PathBuf> {
    let mut current = if start.is_file() { start.parent()? } else { start };
    loop {
        if is_phase1_root(current) {
            return Some(current.to_path_buf());
        }
        current = current.parent()?;
    }
}

fn is_phase1_root(path: &Path) -> bool {
    path.join("config").join("nanobot.example.json").exists()
        && path.join("scripts").join("Start-Phase1Stack.ps1").exists()
}

fn load_source_json(project_root: &Path) -> Result<(Value, PathBuf, bool), String> {
    let local_path = project_root.join("config").join("nanobot.local.json");
    if local_path.exists() {
        return Ok((read_json_file(&local_path)?, local_path, true));
    }

    let example_path = project_root.join("config").join("nanobot.example.json");
    Ok((read_json_file(&example_path)?, example_path, false))
}

fn read_json_file(path: &Path) -> Result<Value, String> {
    let raw = fs::read_to_string(path)
        .map_err(|error| format!("读取 {} 失败: {}", path.display(), error))?;
    serde_json::from_str(raw.trim_start_matches('\u{feff}'))
        .map_err(|error| format!("解析 {} 失败: {}", path.display(), error))
}

fn write_json_with_backup(path: &Path, value: &Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("创建目录 {} 失败: {}", parent.display(), error))?;
    }

    if path.exists() {
        let backup_path = PathBuf::from(format!("{}.bak", path.display()));
        fs::copy(path, &backup_path).map_err(|error| {
            format!(
                "备份配置到 {} 失败: {}",
                backup_path.display(),
                error
            )
        })?;
    }

    let temp_path = PathBuf::from(format!("{}.tmp", path.display()));
    let mut payload = serde_json::to_string_pretty(value)
        .map_err(|error| format!("序列化配置失败: {}", error))?;
    payload.push('\n');
    fs::write(&temp_path, payload)
        .map_err(|error| format!("写入临时配置 {} 失败: {}", temp_path.display(), error))?;

    if path.exists() {
        fs::remove_file(path)
            .map_err(|error| format!("替换旧配置 {} 失败: {}", path.display(), error))?;
    }

    fs::rename(&temp_path, path).map_err(|error| {
        format!(
            "落盘配置 {} 失败: {}",
            path.display(),
            error
        )
    })
}

fn build_command_result(action: &str, output: ProcessOutput, data: Option<Value>) -> CommandResult {
    let summary = if output.success {
        format!("{action}完成。")
    } else {
        format!(
            "{action}返回失败，退出码 {}。",
            output.code.unwrap_or(-1)
        )
    };

    CommandResult {
        ok: output.success,
        summary,
        stdout: output.stdout,
        stderr: output.stderr,
        data,
    }
}

fn run_powershell_script(
    project_root: &Path,
    relative_script: &[&str],
    args: &[&str],
) -> Result<ProcessOutput, String> {
    let script_path = relative_script
        .iter()
        .fold(project_root.to_path_buf(), |path, segment| path.join(segment));

    if !script_path.exists() {
        return Err(format!("脚本不存在: {}", script_path.display()));
    }

    let output = Command::new("powershell.exe")
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-File")
        .arg(&script_path)
        .args(args)
        .env("PHASE1_PROJECT_ROOT", project_root)
        .current_dir(project_root)
        .output()
        .map_err(|error| format!("运行 {} 失败: {}", script_path.display(), error))?;

    Ok(ProcessOutput {
        success: output.status.success(),
        code: output.status.code(),
        stdout: String::from_utf8_lossy(&output.stdout).trim().to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).trim().to_string(),
    })
}

fn get_existing_or_default(root: &Value, path: &[&str], default: &str) -> String {
    let value = get_plain_string(root, path);
    if value.is_empty() {
        default.to_string()
    } else {
        value
    }
}

fn get_plain_string(root: &Value, path: &[&str]) -> String {
    json_at(root, path)
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_default()
}

fn get_resolved_string(root: &Value, path: &[&str], project_root: &Path) -> String {
    get_plain_string(root, path)
        .split('\n')
        .next()
        .map(|value| resolve_placeholders(value, project_root))
        .unwrap_or_default()
}

fn get_bool(root: &Value, path: &[&str], default: bool) -> bool {
    json_at(root, path)
        .and_then(Value::as_bool)
        .unwrap_or(default)
}

fn get_resolved_array(root: &Value, path: &[&str], project_root: &Path) -> Vec<String> {
    let values = json_at(root, path)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(|value| resolve_placeholders(value, project_root))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    dedupe_preserve(values)
}

fn extract_custom_roots(root: &Value, project_root: &Path) -> Vec<String> {
    let base_project = vec![
        project_root.display().to_string(),
        project_root.join("workspace").display().to_string(),
    ];
    let base_attachment = vec![project_root.join("runtime").join("media").display().to_string()];

    let mut base_seen = HashSet::new();
    for item in base_project.iter().chain(base_attachment.iter()) {
        base_seen.insert(normalize_path(item));
    }

    let mut results = Vec::new();
    let mut seen = HashSet::new();
    for path in [
        ["phase1", "project", "allowedRoots"],
        ["phase1", "attachments", "allowedRoots"],
        ["phase1", "artifacts", "allowedRoots"],
        ["phase1", "computerSearch", "allowedRoots"],
    ] {
        for value in get_resolved_array(root, &path, project_root) {
            let normalized = normalize_path(&value);
            if base_seen.contains(&normalized) || normalized.is_empty() {
                continue;
            }
            if seen.insert(normalized) {
                results.push(value);
            }
        }
    }

    results
}

fn detect_local_drive_roots() -> Vec<String> {
    if cfg!(windows) {
        let mut roots = Vec::new();
        for letter in 'C'..='Z' {
            let candidate = format!("{letter}:\\");
            if Path::new(&candidate).exists() {
                roots.push(candidate);
            }
        }
        return roots;
    }
    vec!["/".to_string()]
}

fn matches_full_computer(custom_roots: &[String], system_roots: &[String]) -> bool {
    if custom_roots.is_empty() || system_roots.is_empty() {
        return false;
    }

    let custom_keys: HashSet<String> = custom_roots
        .iter()
        .map(|item| normalize_path(item))
        .filter(|item| !item.is_empty())
        .collect();
    let system_keys: HashSet<String> = system_roots
        .iter()
        .map(|item| normalize_path(item))
        .filter(|item| !item.is_empty())
        .collect();

    custom_keys == system_keys
}

fn merge_roots(base: &[String], custom: &[String]) -> Vec<String> {
    let mut results = Vec::new();
    let mut seen = HashSet::new();
    for item in base.iter().chain(custom.iter()) {
        let trimmed = item.trim();
        if trimmed.is_empty() {
            continue;
        }
        let normalized = normalize_path(trimmed);
        if seen.insert(normalized) {
            results.push(trimmed.to_string());
        }
    }
    results
}

fn dedupe_preserve(values: Vec<String>) -> Vec<String> {
    let mut results = Vec::new();
    let mut seen = HashSet::new();
    for value in values {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            continue;
        }
        let normalized = normalize_path(trimmed);
        if seen.insert(normalized) {
            results.push(trimmed.to_string());
        }
    }
    results
}

fn normalize_path(value: &str) -> String {
    value
        .trim()
        .replace('/', "\\")
        .trim_end_matches('\\')
        .to_ascii_lowercase()
}

fn resolve_placeholders(input: &str, project_root: &Path) -> String {
    let mut output = String::new();
    let chars: Vec<char> = input.chars().collect();
    let mut index = 0usize;

    while index < chars.len() {
        if chars[index] == '$' && index + 1 < chars.len() && chars[index + 1] == '{' {
            let mut end = index + 2;
            while end < chars.len() && chars[end] != '}' {
                end += 1;
            }
            if end < chars.len() {
                let name: String = chars[index + 2..end].iter().collect();
                output.push_str(&resolve_variable(&name, project_root));
                index = end + 1;
                continue;
            }
        }

        output.push(chars[index]);
        index += 1;
    }

    output
}

fn resolve_variable(name: &str, project_root: &Path) -> String {
    if name == "PROJECT_ROOT" {
        return project_root.display().to_string();
    }
    env::var(name).unwrap_or_default()
}

fn json_at<'a>(root: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut current = root;
    for key in path {
        current = current.get(*key)?;
    }
    Some(current)
}

fn set_string(root: &mut Value, path: &[&str], value: String) {
    *ensure_path(root, path) = Value::String(value);
}

fn set_bool(root: &mut Value, path: &[&str], value: bool) {
    *ensure_path(root, path) = Value::Bool(value);
}

fn set_string_array(root: &mut Value, path: &[&str], values: Vec<String>) {
    *ensure_path(root, path) = Value::Array(values.into_iter().map(Value::String).collect());
}

fn ensure_path<'a>(root: &'a mut Value, path: &[&str]) -> &'a mut Value {
    let mut current = root;
    for (index, key) in path.iter().enumerate() {
        let is_last = index + 1 == path.len();
        let object = ensure_object(current);
        if is_last {
            return object.entry((*key).to_string()).or_insert(Value::Null);
        }
        current = object
            .entry((*key).to_string())
            .or_insert_with(|| Value::Object(Map::new()));
    }
    current
}

fn ensure_object(value: &mut Value) -> &mut Map<String, Value> {
    if !value.is_object() {
        *value = Value::Object(Map::new());
    }
    value
        .as_object_mut()
        .expect("value should be an object after initialization")
}
