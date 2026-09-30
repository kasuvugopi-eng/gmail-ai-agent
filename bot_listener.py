import base64
from email import message_from_bytes
import json
import mimetypes
import os
import time

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import requests

load_dotenv()

SCOPES = ["https://mail.google.com/"]
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
if BOT_TOKEN.startswith("bot"):
    BOT_TOKEN = BOT_TOKEN[3:]

creds = Credentials.from_authorized_user_file("token.json", SCOPES)
gmail_service = build("gmail", "v1", credentials=creds)

latest_active_draft_id = None


def answer_callback(callback_query_id: str, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(
            url, json={"callback_query_id": callback_query_id, "text": text}, timeout=10
        )
    except Exception as e:
        print(f"Callback answer error: {e}")


def send_bot_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"Send message error: {e}")


def update_message(chat_id: int, message_id: int, new_text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    try:
        requests.post(
            url,
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": new_text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"Update message error: {e}")


def attach_file_to_draft(draft_id: str, file_path: str, original_filename: str) -> bool:
    try:
        draft_meta = (
            gmail_service.users()
            .drafts()
            .get(userId="me", id=draft_id, format="raw")
            .execute()
        )
        raw_data = base64.urlsafe_b64decode(
            draft_meta["message"]["raw"].encode("ASCII")
        )
        msg = message_from_bytes(raw_data)

        content_type, encoding = mimetypes.guess_type(file_path)
        if content_type is None or encoding is not None:
            content_type = "application/octet-stream"
        main_type, sub_type = content_type.split("/", 1)

        with open(file_path, "rb") as f:
            file_data = f.read()

        msg.add_attachment(
            file_data,
            maintype=main_type,
            subtype=sub_type,
            filename=original_filename,
        )

        raw_updated = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        gmail_service.users().drafts().update(
            userId="me",
            id=draft_id,
            body={"message": {"id": draft_meta["message"]["id"], "raw": raw_updated}},
        ).execute()
        return True
    except Exception as e:
        print(f"Attachment error: {e}")
        return False


def start_listening():
    global latest_active_draft_id
    print("🤖 Telegram Bot Listener with Batch Trash & Reply Support is active...")
    last_update_id = None

    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params = {"timeout": 20, "offset": last_update_id}
            res = requests.get(url, params=params, timeout=25).json()

            for update in res.get("result", []):
                last_update_id = update["update_id"] + 1

                # 1. Document Upload
                if "message" in update and "document" in update["message"]:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    doc = msg["document"]
                    file_id = doc["file_id"]
                    file_name = doc.get("file_name", "attachment.pdf")

                    target_draft_id = None
                    if "reply_to_message" in msg and "reply_markup" in msg["reply_to_message"]:
                        keyboard = msg["reply_to_message"]["reply_markup"].get("inline_keyboard", [])
                        for row in keyboard:
                            for btn in row:
                                if btn.get("callback_data", "").startswith("send:"):
                                    target_draft_id = btn["callback_data"].split("send:")[1]
                                    break

                    if not target_draft_id:
                        target_draft_id = latest_active_draft_id

                    if not target_draft_id:
                        send_bot_message(
                            chat_id,
                            "⚠️ ఏ డ్రాఫ్ట్‌‌కి అటాచ్ చేయాలో గుర్తించలేకపోయాం. దయచేసి బాట్ పంపిన డ్రాఫ్ట్ మెసేజ్ కి 'Reply' ఇచ్చి ఫైల్ పంపండి.",
                        )
                        continue

                    file_info = requests.get(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}",
                        timeout=10,
                    ).json()
                    file_path_on_server = file_info["result"]["file_path"]
                    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path_on_server}"

                    local_filename = f"temp_{file_name}"
                    with requests.get(download_url, stream=True, timeout=20) as r:
                        with open(local_filename, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)

                    success = attach_file_to_draft(target_draft_id, local_filename, file_name)
                    if os.path.exists(local_filename):
                        os.remove(local_filename)

                    if success:
                        send_bot_message(
                            chat_id,
                            f"📎 '{file_name}' draft కి add అయింది! ఇప్పుడు [ ✅ Send Reply ] బటన్ నొక్కండి.",
                        )
                    else:
                        send_bot_message(chat_id, "⚠️ ఫైల్ అటాచ్ చేయలేకపోయాం.")

                # 2. Inline Action Buttons
                elif "callback_query" in update:
                    cb = update["callback_query"]
                    data = cb.get("data", "")
                    cb_id = cb.get("id")
                    chat_id = cb["message"]["chat"]["id"]
                    msg_id = cb["message"]["message_id"]

                    # Action 1: Send Single Draft
                    if data.startswith("send:"):
                        draft_id = data.split("send:")[1]
                        latest_active_draft_id = draft_id
                        try:
                            gmail_service.users().drafts().send(
                                userId="me", body={"id": draft_id}
                            ).execute()
                            answer_callback(cb_id, "Email Sent! ✅")
                            update_message(chat_id, msg_id, "✅ *Email has been sent successfully!*")
                            latest_active_draft_id = None
                        except Exception as e:
                            print(f"Send draft error: {e}")
                            answer_callback(cb_id, "Error sending draft.")

                    # Action 2: Keep Single Draft
                    elif data.startswith("keep:"):
                        draft_id = data.split("keep:")[1]
                        latest_active_draft_id = draft_id
                        answer_callback(cb_id, "Draft preserved in Gmail 📁")
                        update_message(chat_id, msg_id, f"📁 *Draft preserved in Gmail (ID: `{draft_id}`).*")

                    # Action 3: Delete All Promotional Emails
                    elif data == "trash_all":
                        print("🗑️ Trash All request received from Telegram...")
                        if os.path.exists("pending_trash.json"):
                            with open("pending_trash.json", "r") as f:
                                trash_items = json.load(f)

                            count = 0
                            for item in trash_items:
                                mid = item["email_id"] if isinstance(item, dict) else item
                                try:
                                    gmail_service.users().messages().trash(userId="me", id=mid).execute()
                                    count += 1
                                except Exception as err:
                                    print(f"Error trashing {mid}: {err}")

                            os.remove("pending_trash.json")
                            answer_callback(cb_id, f"Deleted {count} emails! 🗑️")
                            update_message(
                                chat_id,
                                msg_id,
                                f"🗑️ *All {count} promotional emails have been moved to Trash successfully!*",
                            )
                            print(f"✅ Successfully trashed {count} emails.")
                        else:
                            answer_callback(cb_id, "No pending emails.")
                            update_message(chat_id, msg_id, "ℹ️ *No pending emails to delete.*")

        except Exception as e:
            print(f"Listener error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    start_listening()