#!/usr/bin/env python3
"""
Google Drive 文件同步脚本 - 将本地 reports 文件夹下的所有文件（含子文件夹）上传到
指定的 Google Drive 文件夹中，并在云盘中同步创建相同的目录结构。

用法：
    python sync.py

首次运行会弹出浏览器进行 OAuth 授权，授权成功后 token.json 会自动保存，
后续运行将静默使用已保存的 token。
"""

import os
import pickle
import logging

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ============ 配置 ============

# 本地待上传的文件夹路径
LOCAL_FOLDER = r"D:\Program\AI_Investment_Lab\reports"

# 谷歌云盘目标文件夹 ID（从 URL 中提取）
# URL: https://drive.google.com/drive/folders/1_nhPYqlNf7Grf2Am-uLNPeFcsfBVEqkS
DRIVE_FOLDER_ID = "1_nhPYqlNf7Grf2Am-uLNPeFcsfBVEqkS"

# OAuth 2.0 凭据文件路径（从 Google Cloud Console 下载）
CREDENTIALS_FILE = "credentials.json"

# 本地 token 缓存文件（首次认证后自动生成，用于后续静默运行）
TOKEN_FILE = "token.json"

# 完整权限 scope
SCOPES = ["https://www.googleapis.com/auth/drive"]

# ============ 日志配置 ============

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def authenticate_google_drive():
    """
    使用 OAuth 2.0 认证并返回 Google Drive 服务对象。

    1. 检查本地 token.json 是否存在且有效。
    2. 若 token 过期则自动刷新。
    3. 若无有效 token，弹出浏览器进行首次 OAuth 授权。
    """
    creds = None

    # 尝试加载已保存的 token
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "rb") as token:
                creds = pickle.load(token)
            logger.info("已加载本地 token 文件 (%s)", TOKEN_FILE)
        except Exception as e:
            logger.warning("读取 token 文件失败，将重新认证: %s", e)

    # 如果凭据无效或已过期，尝试刷新
    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Token 已自动刷新")
            except Exception as e:
                logger.warning("Token 刷新失败，将重新认证: %s", e)
                creds = None
        else:
            creds = None

    # 无有效凭据 -> 首次 OAuth 弹窗认证
    if not creds:
        if not os.path.exists(CREDENTIALS_FILE):
            logger.error(
                "未找到凭据文件 '%s'。请确保已从 Google Cloud Console 下载并重命名。",
                CREDENTIALS_FILE,
            )
            raise FileNotFoundError(
                f"凭据文件 {CREDENTIALS_FILE} 不存在，请先下载并放置在项目根目录。"
            )

        logger.info("首次运行，正在启动浏览器进行 OAuth 授权...")
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)

        # 使用本地服务器方式启动 OAuth 流程（自动打开浏览器）
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
        )
        logger.info("OAuth 授权成功！")

        # 保存 token 供后续静默使用
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)
        logger.info("Token 已保存到 %s，下次运行将自动静默认证", TOKEN_FILE)

    # 构建 Google Drive API 服务
    service = build("drive", "v3", credentials=creds)
    return service


def find_or_create_drive_folder(service, folder_name, parent_id):
    """
    在指定的父文件夹下查找同名子文件夹，如果不存在则创建。

    返回找到或新建的文件夹 ID。
    """
    # 先查找是否已存在
    query = (
        f"name = '{folder_name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name)",
            pageSize=10,
        )
        .execute()
    )
    files = results.get("files", [])

    if files:
        folder_id = files[0]["id"]
        logger.debug("  找到已有子文件夹: %s (ID: %s)", folder_name, folder_id)
        return folder_id

    # 不存在则创建
    logger.info("📁  创建远程子文件夹: %s", folder_name)
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created_folder = (
        service.files()
        .create(body=file_metadata, fields="id, name")
        .execute()
    )
    folder_id = created_folder["id"]
    logger.info("    ✅ 创建成功 (ID: %s)", folder_id)
    return folder_id


def file_exists_in_drive(service, file_name, parent_id):
    """
    检查文件是否已存在于指定的 Drive 文件夹中。
    """
    query = (
        f"name = '{file_name}' "
        f"and '{parent_id}' in parents "
        f"and trashed = false"
    )
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name)",
            pageSize=10,
        )
        .execute()
    )
    files = results.get("files", [])
    return len(files) > 0


def find_file_in_drive(service, file_name, parent_id):
    """
    返回指定父目录下与 file_name 匹配的 Drive 文件对象列表（可能为空）。
    """
    query = (
        f"name = '{file_name}' "
        f"and '{parent_id}' in parents "
        f"and trashed = false"
    )
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name)",
            pageSize=10,
        )
        .execute()
    )
    return results.get("files", [])


def upload_file(service, file_path, parent_id):
    """
    上传单个文件到指定的 Drive 文件夹中。
    """
    file_name = os.path.basename(file_path)

    # 检查是否已存在
    if file_exists_in_drive(service, file_name, parent_id):
        logger.info("⏭️  文件 '%s' 已在云盘中存在，跳过上传", file_name)
        return

    logger.info("⬆️  正在上传: %s ...", file_name)
    media = MediaFileUpload(
        str(file_path),
        resumable=True,
    )
    file_metadata = {
        "name": file_name,
        "parents": [parent_id],
    }

    uploaded_file = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id, name")
        .execute()
    )
    logger.info("✅ 上传成功: %s (ID: %s)", file_name, uploaded_file.get("id"))


def upload_or_update_file(service, file_path, parent_id):
    """
    如果云端已存在同名文件，则使用 files.update 覆盖（保持 fileId 不变）；
    否则执行 create 上传新文件。
    适用于需要每天强制刷新但保留云端 ID 的大表（如 `ticker_data.csv`）。
    """
    file_name = os.path.basename(file_path)
    logger.info("准备同步大表: %s -> Drive Folder ID: %s", file_name, parent_id)

    try:
        files = find_file_in_drive(service, file_name, parent_id)
        media = MediaFileUpload(str(file_path), resumable=True)

        if not files:
            # 不存在 -> create
            logger.info("未检测到云端文件，执行首次上传: %s", file_name)
            file_metadata = {"name": file_name, "parents": [parent_id]}
            created = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id, name")
                .execute()
            )
            logger.info("✅ 首次上传成功: %s (ID: %s)", file_name, created.get("id"))
            print(f"检测到大表 {file_name} 不存在，已上传首次副本 ✅")
            return created.get("id")

        # 存在 -> 取第一个并 update
        file_id = files[0]["id"]
        logger.info("检测到云端已存在 %s (ID: %s)，正在执行覆盖更新...", file_name, file_id)
        print(f"检测到大表 {file_name} 已存在，正在执行云端覆盖更新... ")

        updated = (
            service.files()
            .update(fileId=file_id, media_body=media, fields="id, name")
            .execute()
        )
        logger.info("✅ 覆盖更新成功: %s (ID: %s)", file_name, updated.get("id"))
        print(f"{file_name} 覆盖成功 ✅")
        return updated.get("id")

    except Exception as e:
        logger.error("覆盖上传失败 %s: %s", file_name, e)
        raise


def sync_local_to_drive(service, local_folder, root_drive_id):
    """
    递归遍历本地文件夹，在云盘中同步目录结构并上传所有文件。
    """
    local_folder = os.path.normpath(local_folder)

    if not os.path.exists(local_folder):
        logger.warning("本地文件夹不存在: %s", local_folder)
        return

    if not os.path.isdir(local_folder):
        logger.error("路径不是文件夹: %s", local_folder)
        return

    # 缓存：本地相对路径 -> Drive 文件夹 ID
    # 根目录映射到 root_drive_id
    folder_cache = {"": root_drive_id}

    # 用于统计
    total_files = 0
    total_uploaded = 0

    logger.info("开始递归扫描文件夹: %s", local_folder)

    # 第一遍：先遍历所有子目录，确保云盘目录结构完整建立
    # 第二遍：上传文件（合并在一次 walk 中进行）
    for current_dir, dir_names, file_names in os.walk(local_folder):
        # 计算相对路径（相对于 LOCAL_FOLDER）
        rel_path = os.path.relpath(current_dir, local_folder)
        if rel_path == ".":
            rel_path = ""

        # 获取当前目录对应的 Drive 父文件夹 ID
        parent_drive_id = folder_cache.get(rel_path)
        if parent_drive_id is None:
            # 对于子目录，其父目录的相对路径为 os.path.dirname(rel_path)
            parent_rel = os.path.dirname(rel_path)
            # 父目录的 ID 一定已经在缓存中（因为 os.walk 按深度优先遍历）
            grandparent_id = folder_cache.get(parent_rel)
            if grandparent_id is None:
                logger.error(
                    "❌ 无法定位父文件夹（缓存缺失）: %s (父相对路径: %s)",
                    current_dir,
                    parent_rel,
                )
                continue

            dir_name = os.path.basename(current_dir)
            current_drive_id = find_or_create_drive_folder(
                service, dir_name, grandparent_id
            )
            folder_cache[rel_path] = current_drive_id
            parent_drive_id = current_drive_id

        # 上传当前目录下的所有文件
        if not file_names:
            total_files += 0
            continue

        for file_name in file_names:
            file_path = os.path.join(current_dir, file_name)
            # 确保是文件（跳过符号链接等）
            if not os.path.isfile(file_path):
                continue

            # 跳过 Markdown (.md) 文件，不再上传
            if file_name.lower().endswith(".md"):
                logger.info("⏭️  跳过 Markdown 文件: %s", os.path.join(rel_path, file_name))
                continue

            total_files += 1
            logger.info("📄 发现文件: %s", os.path.join(rel_path, file_name))

            try:
                upload_file(service, file_path, parent_drive_id)
                total_uploaded += 1
            except Exception as e:
                logger.error("❌ 上传失败 '%s': %s", file_name, e)

    logger.info("=" * 50)
    logger.info("🎉 同步完成！共发现 %d 个文件，成功上传 %d 个", total_files, total_uploaded)
    logger.info("=" * 50)


def main():
    logger.info("=" * 50)
    logger.info("Google Drive 文件同步脚本启动")
    logger.info("本地文件夹: %s", LOCAL_FOLDER)
    logger.info("目标云盘文件夹 ID: %s", DRIVE_FOLDER_ID)
    logger.info("=" * 50)

    try:
        service = authenticate_google_drive()
        sync_local_to_drive(service, LOCAL_FOLDER, DRIVE_FOLDER_ID)
        # ---- 强制覆盖同步 ticker_data.csv ----
        # 假定项目根目录为 sync.py 所在目录的上一级
        project_root = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(project_root, "data")
        ticker_path = os.path.join(data_dir, "ticker_data.csv")
        if os.path.isfile(ticker_path):
            try:
                upload_or_update_file(service, ticker_path, DRIVE_FOLDER_ID)
            except Exception as e:
                logger.error("ticker_data.csv 覆盖上传出错: %s", e)
        else:
            logger.warning("本地未找到 ticker_data.csv，跳过强制覆盖同步: %s", ticker_path)
    except FileNotFoundError as e:
        logger.error(str(e))
    except Exception as e:
        logger.error("脚本运行出错: %s", e)
        raise


if __name__ == "__main__":
    main()