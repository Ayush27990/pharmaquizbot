import os
import json
import re
import time
import random
import logging
import asyncio
import io

import PyPDF2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
# LOGGING
# ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ======================
# CONFIG
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = 723919716
INTERVAL = 900  # 15 minutes

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID missing")

client = Groq(api_key=GROQ_API_KEY)
pending_questions = {}

# ======================
# HIGH-YIELD TOPICS
# ======================
TOPICS = [
    "G6PD deficiency and oxidative hemolysis",
    "Pyruvate kinase deficiency",
    "Acute intermittent porphyria",
    "Maple syrup urine disease",
    "Ornithine transcarbamylase deficiency",
    "Warfarin mechanism and vitamin K cycle",
    "Organophosphate poisoning and treatment",
    "Phenytoin toxicity and mechanism",
    "Methotrexate toxicity and leucovorin rescue",
    "Tumor lysis syndrome management",
    "Heparin induced thrombocytopenia",
    "Digoxin toxicity and treatment",
    "Beta blocker overdose management",
    "Serotonin syndrome",
    "Neuroleptic malignant syndrome",
    "Biochemistry of urea cycle disorders",
    "Glycogen storage diseases",
    "Fatty acid oxidation disorders",
    "Lysosomal storage diseases",
    "Aminoacidopathies PKU and tyrosinemia",
    "Pharmacology of statins and side effects",
    "ACE inhibitors mechanism and side effects",
    "Aminoglycoside nephrotoxicity and ototoxicity",
    "Fluoroquinolone mechanism and resistance",
    "Antifungal drugs mechanism amphotericin azoles",
    "Anticoagulants direct oral anticoagulants",
    "Pharmacology of proton pump inhibitors",
    "Antiepileptic drugs mechanism of action",
    "Antipsychotic drugs typical and atypical",
    "Pharmacology of beta blockers selectivity",
]

topic_index = 0

# ======================
# HELPERS
# ======================
def escape_md(text):
    for ch in ["_", "*", "[", "]", "(", ")", "~", "`", ">",
               "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(ch, f"\\{ch}")
    return text

def extract_json(text):
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            if isinstance(result, list) and len(result) > 0:
                return result[0]
        return None
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
        return None

# ======================
# GENERATE MCQ
# ======================
async def generate_mcq(content):
    prompt = f"""You are a NEET PG / USMLE / FMGE expert examiner.

Generate ONE high-yield clinical MCQ based on: {content}

Rules:
- Clinical vignette style (patient scenario)
- 4 options, one definitively correct
- No ambiguous or trick questions
- Explanation must cite mechanism clearly
- Explain why each wrong option is incorrect

Return ONLY this JSON format:
{{
  "question": "A patient presents with...",
  "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
  "answer_index": 0,
  "explanation": "Correct: A because... B is wrong because... C is wrong because... D is wrong because..."
}}"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return extract_json(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"MCQ generation error: {e}")
        return None

# ======================
# VALIDATE MCQ
# ======================
async def validate_mcq(mcq):
    prompt = f"""You are a medical education quality reviewer.

Review this MCQ for accuracy and quality:

Question: {mcq['question']}
Options: {mcq['options']}
Answer index: {mcq['answer_index']}
Explanation: {mcq['explanation']}

Check:
1. Is the correct answer actually correct medically?
2. Is the explanation accurate?
3. Are the wrong options clearly wrong?
4. Is it high yield for NEET PG / FMGE?

Return ONLY JSON:
{{
  "score": 8,
  "is_accurate": true,
  "feedback": "Question is accurate and high yield"
}}

Score 1-10. Only approve score >= 7."""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        return extract_json(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return None

# ======================
# SEND FOR APPROVAL
# ======================
async def send_for_approval(context, mcq, source):
    qid = str(int(time.time()))
    pending_questions[qid] = mcq

    correct_option = mcq["options"][mcq["answer_index"]]

    text = (
        f"📋 *NEW MCQ FOR APPROVAL*\n\n"
        f"📚 Source: {source}\n\n"
        f"*{mcq['question']}*\n\n"
        + "\n".join(mcq["options"]) +
        f"\n\n✅ *Correct: {correct_option}*\n\n"
        f"💡 *Explanation:*\n{mcq['explanation']}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve & Post", callback_data=f"approve_{qid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{qid}")
        ],
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen_{qid}")
        ]
    ])

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ======================
# POST TO CHANNEL
# ======================
async def post_to_channel(context, mcq, topic=""):
    try:
        text_msg = (
            f"📚 *DAILY MCQ*\n\n"
            f"*{mcq['question']}*\n\n"
            + "\n".join(mcq["options"])
        )
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text_msg,
            parse_mode="Markdown"
        )
        await asyncio.sleep(2)

        clean_options = []
        for opt in mcq["options"]:
            if len(opt) > 2 and opt[1] == ")":
                clean_options.append(opt[3:].strip())
            else:
                clean_options.append(opt)

        await context.bot.send_poll(
            chat_id=CHANNEL_ID,
            question=mcq["question"][:300],
            options=clean_options,
            type="quiz",
            correct_option_id=int(mcq["answer_index"]),
            is_anonymous=True
        )
        await asyncio.sleep(2)

        explanation_escaped = escape_md(mcq["explanation"])
        spoiler = f"💡 *Explanation:*\n\n||{explanation_escaped}||"
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=spoiler,
            parse_mode="MarkdownV2"
        )
        logger.info("Successfully posted to channel")

    except Exception as e:
        logger.error(f"Post to channel error: {e}")

# ======================
# SCHEDULED JOB
# ======================
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    global topic_index
    try:
        topic = TOPICS[topic_index % len(TOPICS)]
        topic_index += 1
        logger.info(f"Generating MCQ on: {topic}")

        mcq = await generate_mcq(topic)
        if not mcq:
            logger.error("Failed to generate MCQ")
            return

        review = await validate_mcq(mcq)
        if not review:
            logger.error("Failed to validate MCQ")
            return

        score = review.get("score", 0)
        logger.info(f"MCQ score: {score}")

        if score >= 7:
            await send_for_approval(context, mcq, f"Auto: {topic}")
        else:
            logger.info(f"MCQ rejected (score {score}): {review.get('feedback', '')}")
            mcq2 = await generate_mcq(topic)
            if mcq2:
                await send_for_approval(context, mcq2, f"Auto (retry): {topic}")

    except Exception as e:
        logger.error(f"Scheduled job error: {e}")

# ======================
# CALLBACK HANDLER
# ======================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global topic_index
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        qid = data.replace("approve_", "")
        mcq = pending_questions.get(qid)
        if mcq:
            await post_to_channel(context, mcq)
            pending_questions.pop(qid, None)
            await query.edit_message_text("✅ Posted to channel!")
        else:
            await query.edit_message_text("❌ Question expired.")

    elif data.startswith("reject_"):
        qid = data.replace("reject_", "")
        pending_questions.pop(qid, None)
        await query.edit_message_text("❌ Rejected.")

    elif data.startswith("regen_"):
        qid = data.replace("regen_", "")
        old = pending_questions.get(qid)
        pending_questions.pop(qid, None)
        await query.edit_message_text("🔄 Regenerating...")
        topic = TOPICS[topic_index % len(TOPICS)]
        mcq = await generate_mcq(topic)
        if mcq:
            await send_for_approval(context, mcq, f"Regenerated: {topic}")
        else:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="❌ Failed to regenerate."
            )

# ======================
# PDF HANDLER
# ======================
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("📄 PDF received. Extracting text...")
        file = await update.message.document.get_file()
        file_bytes = await file.download_as_bytearray()
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(bytes(file_bytes)))
        text = ""
        for page in pdf_reader.pages[:10]:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
        if not text.strip():
            await update.message.reply_text("❌ Could not extract text.")
            return
        text = text[:4000]
        await update.message.reply_text("⏳ Generating MCQ from PDF...")
        mcq = await generate_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        review = await validate_mcq(mcq)
        score = review.get("score", 0) if review else 0
        if score >= 7:
            await send_for_approval(context, mcq, "PDF Upload")
        else:
            await update.message.reply_text(
                f"⚠️ MCQ quality too low (score: {score}). Regenerating..."
            )
            mcq2 = await generate_mcq(text)
            if mcq2:
                await send_for_approval(context, mcq2, "PDF Upload (retry)")
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await update.message.reply_text("❌ PDF processing failed.")

# ======================
# COMMANDS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "✅ *Pharma Quiz Bot Running!*\n\n"
        "Features:\n"
        "🤖 Auto MCQ every 15 min\n"
        "✅ AI quality validation\n"
        "👨‍⚕️ Admin approval system\n"
        "🔄 Regenerate option\n"
        "📄 PDF support\n\n"
        "Commands:\n"
        "/postnow - Generate immediately\n"
        "/status - Check bot status",
        parse_mode="Markdown"
    )

async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Generating MCQ...")
    await scheduled_job(context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        f"✅ Bot is running\n"
        f"📊 Pending approvals: {len(pending_questions)}\n"
        f"📚 Topic index: {topic_index}/{len(TOPICS)}"
    )

# ======================
# MAIN
# ======================
def main():
    logger.info("Starting Pharma Quiz Bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    app.job_queue.run_repeating(
        scheduled_job,
        interval=INTERVAL,
        first=10
    )

    logger.info(f"Bot started! Posting every {INTERVAL//60} minutes.")
    app.run_polling()

if __name__ == "__main__":
    main()

