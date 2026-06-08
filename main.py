import os
import json
import re
import time
import random
import logging
import asyncio
import fitz  # PyMuPDF

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from groq import Groq

# ======================
# CONFIG
# ======================
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")

ADMIN_ID = 723919716

client = Groq(api_key=GROQ_API_KEY)

pending_questions = {}

# ======================
# PDF TEXT EXTRACTION
# ======================
def extract_text(pdf_path):
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text[:12000]

# ======================
# PDF IMAGE EXTRACTION
# ======================
def extract_images(pdf_path):
    doc = fitz.open(pdf_path)
    images = []

    for page_index in range(len(doc)):
        for img in doc[page_index].get_images(full=True):
            xref = img[0]
            base = doc.extract_image(xref)
            img_bytes = base["image"]

            img_path = f"/tmp/img_{page_index}_{xref}.png"

            with open(img_path, "wb") as f:
                f.write(img_bytes)

            images.append(img_path)

    return images

# ======================
# JSON PARSER
# ======================
def extract_json(text):
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        return json.loads(match.group())
    except:
        return None

# ======================
# MCQ GENERATOR (GROQ)
# ======================
async def generate_mcq(content, mode="text"):
    prompt = f"""
You are a NEET PG / INICET examiner.

Generate ONE HIGH-YIELD clinical MCQ.

Mode: {mode}

CONTENT:
{content}

Rules:
- Clinical vignette style preferred
- 4 options (A–D)
- Only one correct answer
- Very high difficulty
- Explanation must cover all options

Return ONLY JSON:

{{
 "question": "...",
 "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
 "answer_index": 0,
 "explanation": "..."
}}
"""

    response = client.chat.completions.create(
        model="llama3-70b-8192",
        messages=[{"role": "user", "content": prompt}]
    )

    return extract_json(response.choices[0].message.content)

# ======================
# REVIEWER (QUALITY FILTER)
# ======================
async def review_mcq(mcq):
    prompt = f"""
You are a NEET PG question reviewer.

Evaluate:

Q: {mcq['question']}
Options: {mcq['options']}
Explanation: {mcq['explanation']}

Score 1-10 based on:
- Clinical relevance
- Difficulty
- Distractors quality

Return JSON:
{{
 "score": 0,
 "approved": false
}}
"""

    response = client.chat.completions.create(
        model="llama3-70b-8192",
        messages=[{"role": "user", "content": prompt}]
    )

    return extract_json(response.choices[0].message.content)

# ======================
# ADMIN APPROVAL
# ======================
async def send_for_approval(context, mcq, source):
    qid = str(int(time.time()))
    pending_questions[qid] = mcq

    text = (
        f"📋 NEW HIGH-YIELD MCQ\n\n"
        f"📚 Source: {source}\n\n"
        f"{mcq['question']}\n\n"
        + "\n".join(mcq["options"]) +
        f"\n\n💡 {mcq['explanation']}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{qid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{qid}")
        ]
    ])

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        reply_markup=keyboard
    )

# ======================
# PDF HANDLER
# ======================
async def handle_pdf(update, context):
    if update.effective_user.id != ADMIN_ID:
        return

    file = await update.message.document.get_file()
    path = f"/tmp/{update.message.document.file_name}"
    await file.download_to_drive(path)

    await update.message.reply_text("📄 PDF received. Processing...")

    text = extract_text(path)
    images = extract_images(path)

    # 1️⃣ Generate MCQ from TEXT
    mcq_text = await generate_mcq(text, mode="text")

    # 2️⃣ Generate MCQ from IMAGE if available
    mcq_image = None
    if images:
        mcq_image = await generate_mcq("Image-based medical diagram", mode="image")

    # REVIEW STEP
    for mcq in [mcq_text, mcq_image]:
        if not mcq:
            continue

        review = await review_mcq(mcq)

        if review and review.get("score", 0) >= 8:
            await send_for_approval(context, mcq, "PDF")
        else:
            await update.message.reply_text("⚠️ Low quality MCQ rejected automatically")

# ======================
# CALLBACK HANDLER
# ======================
async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("approve_"):
        qid = data.split("_")[1]
        mcq = pending_questions.get(qid)

        if mcq:
            text = (
                f"📚 APPROVED MCQ\n\n"
                f"{mcq['question']}\n\n"
                + "\n".join(mcq["options"]) +
                f"\n\n💡 Explanation:\n||{mcq['explanation']}||"
            )

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text
            )

        pending_questions.pop(qid, None)
        await query.edit_message_text("✅ Posted to channel")

    elif data.startswith("reject_"):
        qid = data.split("_")[1]
        pending_questions.pop(qid, None)
        await query.edit_message_text("❌ Rejected")

# ======================
# START
# ======================
async def start(update, context):
    await update.message.reply_text(
        "🚀 Bot running:\n\n"
        "✔ PDF MCQ extraction\n"
        "✔ Image MCQ support\n"
        "✔ AI review system\n"
        "✔ Admin approval system"
    )

# ======================
# MAIN
# ======================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
