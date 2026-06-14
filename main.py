import os
import json
import re
import time
import logging
import asyncio
import io
import base64
import random

import PyPDF2
import httpx
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi

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
INTERVAL = 900  

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID missing")

client = Groq(api_key=GROQ_API_KEY)
pending_questions = {}

# ======================
# PERSISTENCE
# ======================
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f)
    except:
        pass

used_topics = load_json("used_topics.json", [])
used_questions = load_json("used_questions.json", [])
last_subject = load_json("last_subject.json", {"subject": "pharma"})
used_neet_chunks = load_json("used_neet_chunks.json", [])
NEET_PHARMA_CHUNKS = []

# ======================
# PDF LOADER
# ======================
def load_pdf_chunks(filepath, chunk_size=3000):
    chunks = []
    try:
        with open(filepath, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            current_chunk = ""
            for page in reader.pages:
                text = page.extract_text() or ""
                current_chunk += text + "\n"
                if len(current_chunk) >= chunk_size:
                    chunks.append(current_chunk[:chunk_size])
                    current_chunk = current_chunk[chunk_size:]
            if current_chunk.strip():
                chunks.append(current_chunk)
        logger.info(f"Loaded {len(chunks)} chunks from {filepath}")
        return chunks
    except Exception as e:
        logger.error(f"PDF load error: {e}")
        return []

# ======================
# TOPIC POOLS
# ======================
HARPER_TOPICS = [
    "Urea cycle disorders and hyperammonemia",
    "Glycolysis regulation and Pasteur effect",
    "HMP shunt and NADPH production",
    "Fatty acid synthesis vs beta-oxidation",
    "Cholesterol synthesis and HMG-CoA reductase",
    "Heme synthesis and porphyrias",
    "Protein folding and chaperones",
    "DNA replication and repair mechanisms",
    "RNA processing and splicing",
    "Enzyme kinetics Km and Vmax",
    "Citric acid cycle regulation",
    "Electron transport chain and oxidative phosphorylation",
    "Gluconeogenesis key enzymes",
    "Glycogen storage diseases",
    "Lipoprotein metabolism HDL LDL",
    "Amino acid catabolism and transamination",
    "Purine and pyrimidine synthesis",
    "Collagen synthesis and scurvy",
    "Hemoglobin structure and cooperativity",
    "Biotransformation and cytochrome P450",
    "Vitamins B1 B2 B3 coenzyme roles",
    "Vitamin B12 and folate metabolism",
    "Iron absorption and transport",
    "Signal transduction cAMP pathway",
    "Calcium signaling and calmodulin",
    "Glycogenolysis and glycogen synthase regulation",
    "Ketone body synthesis and utilization",
    "Sphingolipid metabolism and storage diseases",
    "Mucopolysaccharidoses and lysosomal enzymes",
    "Phenylketonuria and amino acid disorders",
    "Maple syrup urine disease",
    "Homocystinuria and methionine metabolism",
    "Albinism and tyrosine metabolism",
    "Nucleotide salvage pathway",
    "Gout and hyperuricemia biochemistry",
    "Carnitine shuttle and fatty acid transport",
    "Peroxisomal disorders and beta-oxidation",
    "Biotin dependent carboxylases",
    "Pyruvate dehydrogenase complex",
    "Cori cycle and lactate metabolism",
]

GOODMAN_TOPICS = [
    "Warfarin mechanism and vitamin K antagonism",
    "Beta blocker pharmacology and selectivity",
    "ACE inhibitors vs ARBs mechanisms",
    "Diuretics loop vs thiazide vs potassium sparing",
    "Antiepileptic drugs mechanisms",
    "Benzodiazepine vs barbiturate GABA mechanism",
    "Opioid receptors and analgesic ladder",
    "NSAIDs COX selectivity and side effects",
    "Aminoglycoside mechanism and toxicity",
    "Beta lactam antibiotics mechanism and resistance",
    "Antifungal drugs mechanisms azoles vs polyenes",
    "Antituberculosis drugs first line mechanisms",
    "Antidiabetic drugs insulin sensitizers vs secretagogues",
    "Thyroid hormone pharmacology",
    "Corticosteroid pharmacology and adverse effects",
    "Antipsychotic D2 receptor blockade",
    "Antidepressants SSRI vs TCA vs MAOI",
    "Anticholinergic drugs atropine uses",
    "Cholinergic drugs and organophosphate poisoning",
    "Cardiac glycosides digoxin mechanism",
    "Antiarrhythmic drugs Vaughan Williams classification",
    "Statins mechanism and myopathy",
    "Anticoagulants heparin vs LMWH vs DOACs",
    "Cancer chemotherapy alkylating agents",
    "Immunosuppressants cyclosporine tacrolimus",
    "Calcium channel blockers types and uses",
    "Nitrates mechanism and tolerance",
    "Aminophylline and methylxanthines",
    "Proton pump inhibitors vs H2 blockers",
    "Methotrexate mechanism and toxicity",
    "Fluoroquinolone mechanism and resistance",
    "Macrolide antibiotics mechanism and uses",
    "Tetracycline mechanism and contraindications",
    "Chloramphenicol mechanism and grey baby syndrome",
    "Vancomycin mechanism and resistance",
    "Lithium mechanism and toxicity",
    "MAO inhibitors and tyramine interaction",
    "Alpha blocker pharmacology prazosin",
    "Antiviral drugs acyclovir mechanism",
    "Antiretroviral drugs NRTI NNRTI mechanism",
]

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

def extract_json_list(text):
    try:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        return []
    except Exception as e:
        logger.error("JSON list parse error: " + str(e))
        return []

def make_question_hash(question_text):
    return question_text[:80].strip().lower()

def is_question_used(question_text):
    h = make_question_hash(question_text)
    return h in used_questions

def mark_question_used(question_text):
    h = make_question_hash(question_text)
    used_questions.append(h)
    if len(used_questions) > 500:
        used_questions.pop(0)
    save_json("used_questions.json", used_questions)

async def safe_groq_call(**kwargs):
    for attempt in range(3):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                wait = 30 * (attempt + 1)
                logger.warning(f"Rate limit hit, waiting {wait}s...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Groq error: {e}")
                return None
    return None

# ======================
# URL / YOUTUBE HELPERS
# ======================
def extract_youtube_id(url):
    patterns = [
        r"youtube\.com/watch\?v=([^&]+)",
        r"youtu\.be/([^?]+)",
        r"youtube\.com/shorts/([^?]+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

async def get_youtube_transcript(video_id):
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        text = " ".join([t["text"] for t in transcript_list])
        return text[:4000]
    except Exception as e:
        logger.error("YouTube transcript error: " + str(e))
        return None

async def fetch_url_content(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=15) as client_http:
            response = await client_http.get(url, headers=headers, follow_redirects=True)
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:4000]
    except Exception as e:
        logger.error("URL fetch error: " + str(e))
        return None

# ======================
# ALTERNATING SUBJECT
# ======================
def get_next_subject():
    rotation = ["harper", "goodman", "neet_pharma"]
    current = last_subject.get("subject", "goodman")
    try:
        next_subject = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        next_subject = "harper"
    last_subject["subject"] = next_subject
    save_json("last_subject.json", last_subject)
    return next_subject

# ======================
# GENERATE TOPIC
# ======================
async def generate_topic(book=None):
    if book == "harper":
        available = [t for t in HARPER_TOPICS if t not in used_topics]
        if not available:
            used_topics[:] = [t for t in used_topics if t in GOODMAN_TOPICS]
            save_json("used_topics.json", used_topics)
            available = HARPER_TOPICS[:]
        topic = random.choice(available)
        used_topics.append(topic)
        save_json("used_topics.json", used_topics)
        return topic

    elif book == "goodman":
        available = [t for t in GOODMAN_TOPICS if t not in used_topics]
        if not available:
            used_topics[:] = [t for t in used_topics if t in HARPER_TOPICS]
            save_json("used_topics.json", used_topics)
            available = GOODMAN_TOPICS[:]
        topic = random.choice(available)
        used_topics.append(topic)
        save_json("used_topics.json", used_topics)
        return topic

    else:
        all_topics = HARPER_TOPICS + GOODMAN_TOPICS
        available = [t for t in all_topics if t not in used_topics]
        if not available:
            used_topics.clear()
            save_json("used_topics.json", used_topics)
            available = all_topics[:]
        topic = random.choice(available)
        used_topics.append(topic)
        if len(used_topics) > 200:
            used_topics.pop(0)
        save_json("used_topics.json", used_topics)
        return topic

# ======================
# NEET PHARMA CHUNK PICKER
# ======================
async def get_neet_pharma_chunk():
    if not NEET_PHARMA_CHUNKS:
        return None
    available = [i for i in range(len(NEET_PHARMA_CHUNKS))
                 if i not in used_neet_chunks]
    if not available:
        used_neet_chunks.clear()
        save_json("used_neet_chunks.json", used_neet_chunks)
        available = list(range(len(NEET_PHARMA_CHUNKS)))
    idx = random.choice(available)
    used_neet_chunks.append(idx)
    if len(used_neet_chunks) > 300:
        used_neet_chunks.pop(0)
    save_json("used_neet_chunks.json", used_neet_chunks)
    return NEET_PHARMA_CHUNKS[idx]

# ======================
# GENERATE MCQ
# ======================
async def generate_mcq(content, book_context=None, retry=0):
    if book_context == "harper":
        source_context = (
            "Based on Harper's Illustrated Biochemistry 33rd Edition. "
            "Reference Harper's chapter topics, enzyme names, and clinical correlations."
        )
    elif book_context == "goodman":
        source_context = (
            "Based on Goodman & Gilman's Pharmacological Basis of Therapeutics. "
            "Reference drug mechanisms, receptor pharmacology, and clinical applications."
        )
    elif book_context == "neet_pharma":
        source_context = (
            "Based on NEET PG Pharmacology 2025 edition. "
            "Focus on high-yield exam topics, drug mechanisms, receptor pharmacology, "
            "and clinical applications relevant to NEET PG exam."
        )
    else:
        source_context = "Based on standard NEET PG / USMLE medical curriculum."

    prompt = (
        "You are a NEET PG / USMLE / FMGE expert examiner.\n\n"
        + source_context + "\n\n"
        "Generate ONE high-yield clinical MCQ based on: " + content + "\n\n"
        "Rules:\n"
        "- Clinical vignette style with patient scenario\n"
        "- 4 options labeled ONLY as A, B, C, D (no punctuation after letter)\n"
        "- One definitively correct answer\n"
        "- No ambiguous or trick questions\n"
        "- Explanation must cite mechanism clearly\n"
        "- Explain why each wrong option is incorrect\n\n"
        "Return ONLY this JSON:\n"
        '{"question": "A patient presents with...", '
        '"options": ["Unilateral recurrent laryngeal nerve injury", '
        '"Bilateral recurrent laryngeal nerve injury", '
        '"External branch of superior laryngeal nerve injury", '
        '"Internal branch of superior laryngeal nerve injury"], '
        '"answer_index": 0, '
        '"explanation": "Correct: A because... B is wrong because..."}'
        "\n\nIMPORTANT: options must be plain text only, NO A) or A. prefix inside the options array."
    )

    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        if not response:
            return None
        mcq = extract_json(response.choices[0].message.content)
        if not mcq:
            return None

        cleaned_options = []
        for opt in mcq.get("options", []):
            opt = opt.strip()
            if len(opt) > 2 and opt[1] in (")", ".") and opt[0].isalpha():
                opt = opt[2:].strip()
            cleaned_options.append(opt)
        mcq["options"] = cleaned_options

        if is_question_used(mcq.get("question", "")):
            logger.warning("Duplicate question detected, retrying...")
            if retry < 2:
                await asyncio.sleep(5)
                return await generate_mcq(content, book_context, retry=retry + 1)
            else:
                logger.warning("Still duplicate after retries, using anyway")

        mark_question_used(mcq.get("question", ""))
        return mcq

    except Exception as e:
        logger.error("MCQ generation error: " + str(e))
        return None

# ======================
# REPHRASE FORWARDED MCQ
# ======================
async def rephrase_forwarded_mcq(text):
    prompt = (
        "You are a medical MCQ expert.\n\n"
        "Here is a forwarded MCQ:\n\n" + text + "\n\n"
        "Task:\n"
        "1. Slightly rephrase the question stem (keep same meaning)\n"
        "2. Keep the same options\n"
        "3. Identify the correct answer\n"
        "4. Add a detailed explanation\n\n"
        "Return ONLY this JSON:\n"
        '{"question": "rephrased question...", '
        '"options": ["option1", "option2", "option3", "option4"], '
        '"answer_index": 0, '
        '"explanation": "Correct: A because... B is wrong because..."}'
        "\n\nIMPORTANT: options must be plain text only, NO A) or A. prefix inside the options array."
    )
    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        if not response:
            return None
        mcq = extract_json(response.choices[0].message.content)
        if mcq:
            cleaned_options = []
            for opt in mcq.get("options", []):
                opt = opt.strip()
                if len(opt) > 2 and opt[1] in (")", ".") and opt[0].isalpha():
                    opt = opt[2:].strip()
                cleaned_options.append(opt)
            mcq["options"] = cleaned_options
        return mcq
    except Exception as e:
        logger.error("Rephrase error: " + str(e))
        return None

# ======================
# GENERATE MCQ FROM IMAGE
# ======================
async def generate_mcq_from_image(image_bytes, mime_type="image/jpeg"):
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        vision_response = await safe_groq_call(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{b64}"}
                        },
                        {
                            "type": "text",
                            "text": "Extract all medical/biochemistry/pharmacology text from this image. Return raw text only."
                        }
                    ]
                }
            ],
            temperature=0.1
        )
        if not vision_response:
            return None, "Vision model unavailable"
        extracted_text = vision_response.choices[0].message.content
        if not extracted_text or len(extracted_text.strip()) < 20:
            return None, "Could not extract text from image"
        extracted_text = extracted_text[:4000]
        await asyncio.sleep(20)
        mcq = await generate_mcq(extracted_text)
        return mcq, extracted_text[:200]
    except Exception as e:
        logger.error("Image MCQ error: " + str(e))
        return None, str(e)

# ======================
# SEND FOR APPROVAL
# ======================
async def send_for_approval(bot, mcq, source):
    try:
        qid = str(int(time.time()))
        pending_questions[qid] = {"mcq": mcq, "source": source}
        correct_option = mcq["options"][mcq["answer_index"]]

        options_preview = []
        for i, opt in enumerate(mcq["options"]):
            letter = chr(65 + i)
            marker = "✅ " if i == mcq["answer_index"] else ""
            options_preview.append(f"{marker}{letter}. {opt}")

        text = (
            "📋 NEW MCQ FOR APPROVAL\n\n"
            "📚 Source: " + source + "\n\n"
            + mcq["question"] + "\n\n"
            + "\n".join(options_preview)
            + "\n\n💡 Explanation:\n" + mcq["explanation"]
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve & Post", callback_data="approve_" + qid),
                InlineKeyboardButton("❌ Reject", callback_data="reject_" + qid)
            ],
            [
                InlineKeyboardButton("🔄 Regenerate", callback_data="regen_" + qid)
            ]
        ])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            reply_markup=keyboard
        )
        logger.info("MCQ sent for approval: " + source)
    except Exception as e:
        logger.error("Send for approval error: " + str(e))

# ======================
# POST TO CHANNEL
# ======================
async def post_to_channel(bot, mcq):
    try:
        options_text = []
        for i, opt in enumerate(mcq["options"]):
            letter = chr(65 + i)
            options_text.append(f"{letter}. {opt}")

        text_msg = mcq["question"] + "\n\n" + "\n".join(options_text)
        await bot.send_message(chat_id=CHANNEL_ID, text=text_msg)
        await asyncio.sleep(2)
    except Exception as e:
        logger.error("Failed to send question text: " + str(e))

    try:
        clean_options = [opt[:100] for opt in mcq["options"]]
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=mcq["question"][:300],
            options=clean_options,
            type="quiz",
            correct_option_id=int(mcq["answer_index"]),
            is_anonymous=True
        )
        await asyncio.sleep(2)
    except Exception as e:
        logger.error("Failed to send poll: " + str(e))

    try:
        explanation_escaped = escape_md(mcq["explanation"])
        spoiler = "💡 *Explanation:*\n\n||" + explanation_escaped + "||"
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=spoiler,
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.error("Failed to send explanation (MarkdownV2): " + str(e))
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text="💡 Explanation:\n\n" + mcq["explanation"]
            )
        except Exception as e2:
            logger.error("Fallback explanation failed: " + str(e2))

# ======================
# SCHEDULED JOB
# ======================
async def scheduled_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        subject = get_next_subject()
        logger.info(f"Scheduled job — subject: {subject}")

        if subject == "neet_pharma":
            chunk = await get_neet_pharma_chunk()
            if not chunk:
                logger.error("No NEET Pharma chunks available")
                return
            await asyncio.sleep(20)
            mcq = await generate_mcq(chunk, book_context="neet_pharma")
            if not mcq:
                logger.error("Failed to generate MCQ")
                return
            await send_for_approval(context.bot, mcq, "NEET PG Pharmacology 2025")
        else:
            topic = await generate_topic(book=subject)
            logger.info("Generated topic: " + topic)
            await asyncio.sleep(20)
            mcq = await generate_mcq(topic, book_context=subject)
            if not mcq:
                logger.error("Failed to generate MCQ")
                return
            label = "Harper 33e" if subject == "harper" else "Goodman & Gilman"
            await send_for_approval(context.bot, mcq, f"{label}: {topic}")

    except Exception as e:
        logger.error("Scheduled job error: " + str(e))

# ======================
# CALLBACK HANDLER
# ======================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        qid = data.replace("approve_", "")
        item = pending_questions.get(qid)
        if item:
            await post_to_channel(context.bot, item["mcq"])
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
        old_item = pending_questions.pop(qid, None)
        await query.edit_message_text("🔄 Regenerating...")

        source = old_item["source"] if old_item else ""
        if "Harper" in source:
            book = "harper"
        elif "Goodman" in source:
            book = "goodman"
        elif "NEET" in source:
            book = "neet_pharma"
        else:
            book = get_next_subject()

        if book == "neet_pharma":
            chunk = await get_neet_pharma_chunk()
            if not chunk:
                await context.bot.send_message(chat_id=ADMIN_ID, text="❌ No NEET chunks. Try /postnow")
                return
            await asyncio.sleep(20)
            mcq = await generate_mcq(chunk, book_context="neet_pharma")
            if mcq:
                await send_for_approval(context.bot, mcq, "NEET PG Pharmacology 2025")
            else:
                await context.bot.send_message(chat_id=ADMIN_ID, text="❌ Failed to regenerate. Try /postnow")
        else:
            topic = await generate_topic(book=book)
            await asyncio.sleep(20)
            mcq = await generate_mcq(topic, book_context=book)
            if mcq:
                label = "Harper 33e" if book == "harper" else "Goodman & Gilman"
                await send_for_approval(context.bot, mcq, f"{label}: {topic}")
            else:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text="❌ Failed to regenerate. Try /postnow"
                )

# ======================
# FORWARDED POLL HANDLER
# ======================
async def handle_forwarded_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        poll = update.message.poll
        if not poll:
            return
        question = poll.question
        options = [opt.text for opt in poll.options]
        text = (
            question + "\n\n"
            + "\n".join([chr(65 + i) + ") " + opt for i, opt in enumerate(options)])
        )
        await update.message.reply_text("📊 Forwarded poll detected! Processing...")
        mcq = await rephrase_forwarded_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Could not process poll.")
            return
        await send_for_approval(context.bot, mcq, "Forwarded Poll")
    except Exception as e:
        logger.error("Forwarded poll error: " + str(e))
        await update.message.reply_text("❌ Failed to process poll.")

# ======================
# IMAGE HANDLER
# ======================
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("🖼️ Image received. Generating MCQ...")
        if update.message.photo:
            file = await update.message.photo[-1].get_file()
        elif update.message.document:
            file = await update.message.document.get_file()
        else:
            return
        image_bytes = bytes(await file.download_as_bytearray())
        mcq, preview = await generate_mcq_from_image(image_bytes)
        if not mcq:
            await update.message.reply_text(f"❌ Failed. Reason: {preview}")
            return
        await send_for_approval(context.bot, mcq, "Image Upload")
    except Exception as e:
        logger.error("Image handler error: " + str(e))
        await update.message.reply_text("❌ Image processing failed: " + str(e))

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
        await asyncio.sleep(20)
        mcq = await generate_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        await send_for_approval(context.bot, mcq, "PDF Upload")
    except Exception as e:
        logger.error("PDF error: " + str(e))
        await update.message.reply_text("❌ PDF processing failed.")

# ======================
# TEXT HANDLER
# ======================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text.strip()

    if text.startswith("http://") or text.startswith("https://"):
        youtube_id = extract_youtube_id(text)

        if youtube_id:
            await update.message.reply_text("🎥 YouTube link! Fetching transcript...")
            transcript = await get_youtube_transcript(youtube_id)
            if not transcript:
                await update.message.reply_text("❌ No transcript. Trying page content...")
                transcript = await fetch_url_content(text)
            if not transcript:
                await update.message.reply_text("❌ Could not extract content.")
                return
            await update.message.reply_text("⏳ Generating MCQ from video...")
            await asyncio.sleep(20)
            mcq = await generate_mcq(transcript)
            source = "YouTube: " + text[:50]
        else:
            await update.message.reply_text("🔗 Article URL! Fetching content...")
            content = await fetch_url_content(text)
            if not content:
                await update.message.reply_text("❌ Could not fetch content.")
                return
            await update.message.reply_text("⏳ Generating MCQ from article...")
            await asyncio.sleep(20)
            mcq = await generate_mcq(content)
            source = "Article: " + text[:50]

        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        await send_for_approval(context.bot, mcq, source)

    else:
        await update.message.reply_text("💬 Forwarded MCQ text detected! Processing...")
        mcq = await rephrase_forwarded_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Could not process MCQ.")
            return
        await send_for_approval(context.bot, mcq, "Forwarded MCQ")

# ======================
# COMMANDS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Start command from user ID: {user_id}")
    if user_id != ADMIN_ID:
        await update.message.reply_text(f"⛔ Unauthorized. Your ID: {user_id}")
        return
    await update.message.reply_text(
        "✅ Pharma Quiz Bot Running!\n\n"
        "📚 Book Commands:\n"
        "/harper - MCQ from Harper Biochemistry 33rd Ed\n"
        "/goodman - MCQ from Goodman & Gilman\n"
        "/neetpharma - MCQ from NEET PG Pharmacology 2025\n\n"
        "🤖 Other Commands:\n"
        "/postnow - Generate next alternating MCQ\n"
        "/status - Check bot status\n"
        "/debug - Test bot connectivity\n"
        "/resettopics - Clear used topics\n"
        "/resetquestions - Clear used questions\n\n"
        "📎 Send:\n"
        "📝 Forwarded MCQ text → rephrase & post\n"
        "📊 Forwarded MCQ poll → rephrase & post\n"
        "📄 PDF → extract & generate MCQ\n"
        "🖼 Image → analyze & generate MCQ\n"
        "🔗 Article URL → scrape & generate MCQ\n"
        "🎥 YouTube URL → transcript & generate MCQ\n\n"
        "🔄 Scheduled MCQs rotate: Biochem → Pharma → NEET Pharma → ..."
    )

async def harper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("📖 Generating MCQ from Harper's Biochemistry 33rd Edition...")
    topic = await generate_topic(book="harper")
    await update.message.reply_text(f"🧬 Topic: {topic}")
    await asyncio.sleep(20)
    mcq = await generate_mcq(topic, book_context="harper")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, f"Harper 33e: {topic}")

async def goodman_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("💊 Generating MCQ from Goodman & Gilman...")
    topic = await generate_topic(book="goodman")
    await update.message.reply_text(f"💉 Topic: {topic}")
    await asyncio.sleep(20)
    mcq = await generate_mcq(topic, book_context="goodman")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, f"Goodman & Gilman: {topic}")

async def neet_pharma_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not NEET_PHARMA_CHUNKS:
        await update.message.reply_text("❌ NEET Pharma PDF not loaded. Check books/ folder on Railway.")
        return
    await update.message.reply_text(
        f"💊 Generating MCQ from NEET PG Pharmacology 2025...\n"
        f"📚 {len(NEET_PHARMA_CHUNKS)} chunks available"
    )
    chunk = await get_neet_pharma_chunk()
    await asyncio.sleep(20)
    mcq = await generate_mcq(chunk, book_context="neet_pharma")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, "NEET PG Pharmacology 2025")

async def reset_topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    used_topics.clear()
    save_json("used_topics.json", used_topics)
    await update.message.reply_text("✅ Used topics cleared! All topics available again.")

async def reset_questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    used_questions.clear()
    save_json("used_questions.json", used_questions)
    await update.message.reply_text("✅ Used questions cleared! Fresh questions will be generated.")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rotation = ["harper", "goodman", "neet_pharma"]
    current = last_subject.get("subject", "goodman")
    try:
        next_up = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        next_up = "harper"
    await update.message.reply_text(
        f"🔧 Debug Info\n"
        f"Your ID: {user_id}\n"
        f"Admin ID: {ADMIN_ID}\n"
        f"Match: {'✅' if user_id == ADMIN_ID else '❌'}\n"
        f"Pending approvals: {len(pending_questions)}\n"
        f"Topics used: {len(used_topics)}\n"
        f"Questions used: {len(used_questions)}\n"
        f"Next scheduled subject: {next_up}\n"
        f"Harper remaining: {len([t for t in HARPER_TOPICS if t not in used_topics])}/40\n"
        f"Goodman remaining: {len([t for t in GOODMAN_TOPICS if t not in used_topics])}/40\n"
        f"NEET Pharma chunks: {len(NEET_PHARMA_CHUNKS)} loaded, {len(used_neet_chunks)} used"
    )

async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Generating MCQ... please wait 30-60 seconds")
    await scheduled_job(context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rotation = ["harper", "goodman", "neet_pharma"]
    current = last_subject.get("subject", "goodman")
    try:
        next_up = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        next_up = "harper"
    await update.message.reply_text(
        "✅ Bot is running\n"
        "📊 Pending approvals: " + str(len(pending_questions)) + "\n"
        "📚 Topics used: " + str(len(used_topics)) + "\n"
        "❓ Questions used: " + str(len(used_questions)) + "\n"
        f"🔄 Next subject: {next_up}\n"
        f"🧬 Harper remaining: {len([t for t in HARPER_TOPICS if t not in used_topics])}/40\n"
        f"💊 Goodman remaining: {len([t for t in GOODMAN_TOPICS if t not in used_topics])}/40\n"
        f"📖 NEET Pharma: {len(NEET_PHARMA_CHUNKS)} chunks, {len(used_neet_chunks)} used"
    )

# ======================
# MAIN
# ======================
def main():
    global NEET_PHARMA_CHUNKS
    NEET_PHARMA_CHUNKS = load_pdf_chunks("books/neet_pharma.pdf")

    logger.info(f"NEET Pharma chunks loaded: {len(NEET_PHARMA_CHUNKS)}")

    logger.info("Starting Pharma Quiz Bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("harper", harper_command))
    app.add_handler(CommandHandler("goodman", goodman_command))
    app.add_handler(CommandHandler("neetpharma", neet_pharma_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("resettopics", reset_topics_command))
    app.add_handler(CommandHandler("resetquestions", reset_questions_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.POLL, handle_forwarded_poll))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(
        scheduled_job,
        interval=INTERVAL,
        first=30
    )

    logger.info("Bot started! Interval: " + str(INTERVAL // 60) + " minutes")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
