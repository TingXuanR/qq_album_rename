# -*- coding: utf-8 -*-
import os
import re
import subprocess
from pathlib import Path
from datetime import datetime
import threading
import PySimpleGUI as sg
from queue import Queue
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# ==================== 配置 ====================
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.heic'}
VID_EXT = {'.mp4', '.mov'}
ALL_EXT = IMG_EXT | VID_EXT
BASE_HOUR = 12
BASE_MINUTE = 0
BASE_SECOND = 0
MAX_WORKERS = 8  # 多线程最大数量

# ==================== GUI ====================
sg.theme('DarkBlue3')
layout = [
    [sg.Text("请选择要处理的根目录：")],
    [sg.Input(key="-FOLDER-"), sg.FolderBrowse()],
    [sg.Checkbox("是否备份原文件", default=True, key="-BACKUP-")],
    [sg.Button("开始处理"), sg.Button("退出")],
    [sg.Text("处理进度：", key="-PROG-TEXT-")],
    [sg.ProgressBar(100, orientation='h', size=(50, 20), key='-PROGRESS-')],
    [sg.Multiline(size=(100, 20), key='-LOG-', autoscroll=True, disabled=True)]
]
window = sg.Window("全量批量修改照片/视频 EXIF 与重命名（备份版）", layout, finalize=True)

# ==================== 工具函数 ====================
def log(msg):
    window['-LOG-'].update(f"{msg}\n", append=True)
    print(msg)

def get_exiftool_cmd():
    import shutil
    cmd = shutil.which("exiftool")
    if not cmd:
        sg.popup_error("未检测到 exiftool，请安装或加入 PATH")
        sys.exit(1)
    return cmd

EXIFTOOL_CMD = get_exiftool_cmd()

def build_datetime(date_str, seq):
    second = BASE_SECOND + seq
    mm = BASE_MINUTE + second // 60
    ss = second % 60
    hh = BASE_HOUR + mm // 60
    mm = mm % 60
    yyyy, MM, dd = date_str[0:4], date_str[4:6], date_str[6:8]
    exif_dt = f"{yyyy}:{MM}:{dd} {hh:02d}:{mm:02d}:{ss:02d}"
    filename_dt = f"{date_str}_{hh:02d}{mm:02d}{ss:02d}"
    return exif_dt, filename_dt

def has_enough_space(src_folder: Path) -> bool:
    total_size = sum(f.stat().st_size for f in src_folder.rglob("*") if f.is_file())
    disk_free = shutil.disk_usage(str(src_folder.parent)).free
    return disk_free > total_size

def backup_folder(folder_path: Path):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = folder_path.parent / f"Backup{timestamp}"
    if not has_enough_space(folder_path):
        sg.popup_error(f"磁盘空间不足，无法备份 {folder_path}")
        sys.exit(1)
    shutil.copytree(folder_path, backup_dir)
    log(f"[备份完成] {folder_path} -> {backup_dir}")

def get_file_original_time(file_path: Path):
    ext = file_path.suffix.lower()
    exif_time_str = ""
    if ext in IMG_EXT:
        tag = "-DateTimeOriginal"
    elif ext in VID_EXT:
        tag = "-MediaCreateDate"
    else:
        return ""
    try:
        res = subprocess.run(
            [EXIFTOOL_CMD, tag, "-s", "-s", "-s", str(file_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        )
        exif_time_str = res.stdout.strip()
    except:
        exif_time_str = ""
    return exif_time_str

def process_file(file_path: Path, date_str, comment, seq, existing_names, progress_queue):
    exif_time_str = get_file_original_time(file_path)
    if exif_time_str:
        try:
            exif_dt = exif_time_str
            dt_obj = datetime.strptime(exif_dt, "%Y:%m:%d %H:%M:%S")
            filename_dt = dt_obj.strftime("%Y%m%d_%H%M%S")
        except:
            exif_dt, filename_dt = build_datetime(date_str, seq)
    else:
        exif_dt, filename_dt = build_datetime(date_str, seq)

    ext = file_path.suffix.lower()
    new_name = f"IMG_{filename_dt}{ext}"
    counter = 1
    base_name = new_name
    existing_names = existing_names or set()
    while new_name in existing_names:
        new_name = f"{Path(base_name).stem}_{counter}{ext}"
        counter += 1
    existing_names.add(new_name)
    new_path = file_path.parent / new_name

    cmd = [EXIFTOOL_CMD,
           f"-AllDates={exif_dt}",
           f"-CreateDate={exif_dt}",
           f"-ModifyDate={exif_dt}",
           f"-FileModifyDate={exif_dt}"]
    if ext in IMG_EXT:
        cmd.append(f"-DateTimeOriginal={exif_dt}")
        if comment:
            cmd.append(f"-ImageDescription={comment}")
            cmd.append(f"-XPComment={comment}")
    if ext in VID_EXT:
        cmd += [f"-MediaCreateDate={exif_dt}", f"-TrackCreateDate={exif_dt}"]
        if comment:
            cmd.append(f"-Comment={comment}")
    cmd += ["-overwrite_original", str(file_path)]

    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except Exception as e:
        log(f"[EXIF写入失败] {file_path}: {e}")

    try:
        file_path.rename(new_path)
    except Exception as e:
        log(f"[重命名失败] {file_path} -> {new_path}: {e}")

    try:
        dt_obj = datetime.strptime(exif_dt, "%Y:%m:%d %H:%M:%S")
        mod_time = dt_obj.timestamp()
        os.utime(new_path, (mod_time, mod_time))
    except Exception as e:
        log(f"[修改文件时间失败] {new_path}: {e}")

    progress_queue.put(1)

def process_folder(folder_path: Path, progress_queue, backup_enabled=True,
                   parent_date=None, parent_comment="", is_root=False):
    folder_name = folder_path.name
    m = re.match(r"^(\d{8})(?:-(.*))?$", folder_name)
    if m:
        date_str = m.group(1)
        comment = m.group(2) or ""
    else:
        date_str = parent_date
        comment = parent_comment
        log(f"[注意] 目录名不符合格式，使用父目录日期/注释: {folder_name}")

    log(f"\n处理目录: {folder_name} 日期: {date_str} 注释: {comment}")

    if backup_enabled and is_root:
        backup_folder(folder_path)

    files = [p for p in folder_path.iterdir() if p.is_file() and p.suffix.lower() in ALL_EXT]
    files.sort(key=lambda x: x.stat().st_mtime)

    existing_names = set()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for idx, file_path in enumerate(files):
            executor.submit(process_file, file_path, date_str, comment, idx, existing_names, progress_queue)

    for sub in folder_path.iterdir():
        if sub.is_dir():
            process_folder(sub, progress_queue, backup_enabled, date_str, comment, is_root=False)

def worker_thread(folder_path: Path, backup_enabled: bool, progress_queue: Queue):
    all_dirs = [d for d in folder_path.rglob("*") if d.is_dir()]
    all_dirs.insert(0, folder_path)
    total_files = sum(len([p for p in d.rglob("*") if p.suffix.lower() in ALL_EXT]) for d in all_dirs)
    progress_queue.put(("total", total_files))

    for idx, d in enumerate(all_dirs):
        process_folder(d, progress_queue, backup_enabled, is_root=(idx == 0))

# ==================== 主循环 ====================
worker = None
finished_flag = False
progress_queue = None

def update_progress(progress_queue):
    global finished_flag
    count = 0
    total_files = None
    while True:
        try:
            item = progress_queue.get(timeout=0.1)
            if isinstance(item, tuple) and item[0] == "total":
                total_files = item[1]
                count = 0
                finished_flag = False
                window['-PROG-TEXT-'].update(f"处理进度：0/{total_files}")
                window['-PROGRESS-'].update(current_count=0, max=total_files)
            else:
                count += 1
                if total_files:
                    window['-PROG-TEXT-'].update(f"处理进度：{count}/{total_files}")
                    window['-PROGRESS-'].update(current_count=count)
        except:
            time.sleep(0.05)
        if worker and not worker.is_alive() and not finished_flag:
            log("\n[全部处理完成]")
            finished_flag = True

while True:
    event, values = window.read(timeout=100)
    if event in (sg.WINDOW_CLOSED, "退出"):
        break
    if event == "开始处理":
        folder = values["-FOLDER-"]
        backup_enabled = values.get("-BACKUP-", True)
        if not folder or not os.path.exists(folder):
            log("请选择有效的文件夹")
            continue

        folder_path = Path(folder)
        progress_queue = Queue()
        worker = threading.Thread(target=worker_thread, args=(folder_path, backup_enabled, progress_queue), daemon=True)
        worker.start()
        threading.Thread(target=update_progress, args=(progress_queue,), daemon=True).start()
        log("开始处理，请耐心等待...")

window.close()