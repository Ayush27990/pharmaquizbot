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
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
CHANNEL_ID     = os.getenv("CHANNEL_ID")
ADMIN_ID       = 723919716
INTERVAL       = 900          # 15 minutes

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID missing")

groq_client = Groq(api_key=GROQ_API_KEY)

# ======================
# PERSISTENCE HELPERS
# ======================
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"save_json error ({filename}): {e}")

# ======================
# STATE  (loaded at startup, kept in memory, mirrored to disk after every write)
# ======================
used_topics       = load_json("used_topics.json",       [])
used_questions    = load_json("used_questions.json",    [])
last_subject      = load_json("last_subject.json",      {"subject": "pharma"})
used_neet_chunks  = load_json("used_neet_chunks.json",  [])

# pending_questions: short_id (≤8 chars) → full data dict
# We keep short IDs so callback_data NEVER exceeds Telegram's 64-byte hard limit.
pending_questions: dict = load_json("pending_questions.json", {})

def save_pending():
    save_json("pending_questions.json", pending_questions)

NEET_PHARMA_CHUNKS: list[str] = []

# ======================
# SHORT ID  (≤8 chars — fits safely inside 64-byte callback_data)
# ======================
_id_counter = 0

def make_short_id() -> str:
    """Return a collision-resistant 6-char alphanumeric ID."""
    global _id_counter
    _id_counter += 1
    # base-36 encode (timestamp low bits + counter + random nibble)
    n = (int(time.time()) & 0xFFFF) ^ (_id_counter << 4) ^ random.randint(0, 15)
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while n:
        result = chars[n % 36] + result
        n //= 36
    return (result or "0").zfill(6)[:8]   # always 6–8 chars

# ======================
# PDF LOADER
# ======================
def load_pdf_chunks(filepath: str, chunk_size: int = 3000) -> list[str]:
    chunks = []
    try:
        with open(filepath, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            current = ""
            for page in reader.pages:
                text = page.extract_text() or ""
                current += text + "\n"
                while len(current) >= chunk_size:
                    chunks.append(current[:chunk_size])
                    current = current[chunk_size:]
            if current.strip():
                chunks.append(current)
        logger.info(f"Loaded {len(chunks)} chunks from {filepath}")
    except Exception as e:
        logger.error(f"PDF load error: {e}")
    return chunks

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
def escape_md(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def extract_json(text: str):
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            result = json.loads(m.group())
            if isinstance(result, list) and result:
                return result[0]
    except Exception as e:
        logger.error(f"JSON parse error: {e}")
    return None

def make_question_hash(q: str) -> str:
    return q[:80].strip().lower()

def is_question_used(q: str) -> bool:
    return make_question_hash(q) in used_questions

def mark_question_used(q: str):
    h = make_question_hash(q)
    used_questions.append(h)
    if len(used_questions) > 500:
        used_questions.pop(0)
    save_json("used_questions.json", used_questions)

def clean_options(options: list) -> list:
    """Strip any A) / A. prefixes the model may have added inside option text."""
    cleaned = []
    for opt in options:
        opt = opt.strip()
        if len(opt) > 2 and opt[1] in (")", ".") and opt[0].isalpha():
            opt = opt[2:].strip()
        cleaned.append(opt)
    return cleaned

# ======================
# GROQ WRAPPER  (non-blocking retry)
# ======================
async def safe_groq_call(**kwargs):
    for attempt in range(4):
        try:
            # Run the synchronous Groq call in a thread so it never blocks the event loop
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: groq_client.chat.completions.create(**kwargs)
            )
            return response
        except Exception as e:
            err = str(e).lower()
            if "rate_limit" in err or "429" in err:
                wait = 30 * (attempt + 1)
                logger.warning(f"Rate limit (attempt {attempt+1}), waiting {wait}s…")
                await asyncio.sleep(wait)
            elif any(x in err for x in ("timeout", "connection", "503", "502", "overload")):
                wait = 15 * (attempt + 1)
                logger.warning(f"Transient error (attempt {attempt+1}): {e} — retrying in {wait}s…")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Groq fatal error: {e}")
                return None
    logger.error("safe_groq_call: all attempts exhausted")
    return None

# ======================
# URL / YOUTUBE
# ======================
def extract_youtube_id(url: str):
    for pat in [r"youtube\.com/watch\?v=([^&]+)",
                r"youtu\.be/([^?]+)",
                r"youtube\.com/shorts/([^?]+)"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

async def get_youtube_transcript(video_id: str):
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(t["text"] for t in transcript_list)[:4000]
    except Exception as e:
        logger.error(f"YouTube transcript error: {e}")
        return None

async def fetch_url_content(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=15) as hc:
            r = await hc.get(url, headers=headers, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:4000]
    except Exception as e:
        logger.error(f"URL fetch error: {e}")
        return None

# ======================
# SUBJECT ROTATION
# ======================
def get_next_subject() -> str:
    rotation = ["harper", "goodman", "neet_pharma"]
    current  = last_subject.get("subject", "goodman")
    try:
        nxt = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        nxt = "harper"
    last_subject["subject"] = nxt
    save_json("last_subject.json", last_subject)
    return nxt

# ======================
# TOPIC PICKER
# ======================
async def generate_topic(book: str = None) -> str:
    if book == "harper":
        pool = HARPER_TOPICS
    elif book == "goodman":
        pool = GOODMAN_TOPICS
    else:
        pool = HARPER_TOPICS + GOODMAN_TOPICS

    available = [t for t in pool if t not in used_topics]
    if not available:
        # Reset only the used entries that belong to this pool
        used_topics[:] = [t for t in used_topics if t not in pool]
        save_json("used_topics.json", used_topics)
        available = pool[:]

    topic = random.choice(available)
    used_topics.append(topic)
    if len(used_topics) > 300:
        used_topics.pop(0)
    save_json("used_topics.json", used_topics)
    return topic

# ======================
# NEET CHUNK PICKER
# ======================
async def get_neet_pharma_chunk() -> str | None:
    if not NEET_PHARMA_CHUNKS:
        return None
    available = [i for i in range(len(NEET_PHARMA_CHUNKS)) if i not in used_neet_chunks]
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
# MCQ GENERATION
# ======================
async def generate_mcq(content: str, book_context: str = None, retry: int = 0,
                       auto_select_topic: bool = False):
    ctx_map = {
        "harper":      "Based on Harper's Illustrated Biochemistry 33rd Edition. Reference Harper's chapter topics, enzyme names, and clinical correlations.",
        "goodman":     "Based on Goodman & Gilman's Pharmacological Basis of Therapeutics. Reference drug mechanisms, receptor pharmacology, and clinical applications.",
        "neet_pharma": "Based on NEET PG Pharmacology 2025. Focus on high-yield exam topics relevant to NEET PG.",
    }
    source_context = ctx_map.get(book_context, "Based on standard NEET PG / USMLE medical curriculum.")

    if auto_select_topic:
        # Pick a topic internally so we only need one API call total
        topic = await generate_topic(book=book_context)
        mcq_content = topic
    else:
        topic = None
        mcq_content = content

    prompt = (
        "You are a NEET PG / USMLE / FMGE expert examiner.\n\n"
        + source_context + "\n\n"
        "Generate ONE high-yield clinical MCQ based on: " + mcq_content + "\n\n"
        "Rules:\n"
        "- Clinical vignette style with patient scenario\n"
        "- 4 options labeled ONLY as A, B, C, D (no punctuation after letter)\n"
        "- One definitively correct answer\n"
        "- No ambiguous or trick questions\n"
        "- Explanation must cite mechanism clearly\n"
        "- Explain why each wrong option is incorrect\n\n"
        "Return ONLY this JSON (no markdown, no preamble):\n"
        '{"question": "A patient presents with...", '
        '"options": ["Option text only", "Option text only", "Option text only", "Option text only"], '
        '"answer_index": 0, '
        '"explanation": "Correct: A because... B is wrong because..."}\n\n'
        "CRITICAL: options array must contain plain text strings ONLY. "
        "Do NOT include A) B) C) D) or A. B. C. D. prefixes inside the options array."
    )

    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        if not response:
            return None

        raw = response.choices[0].message.content
        mcq = extract_json(raw)
        if not mcq:
            logger.error(f"Could not parse MCQ JSON. Raw:\n{raw[:300]}")
            return None

        # Validate structure
        if not all(k in mcq for k in ("question", "options", "answer_index", "explanation")):
            logger.error("MCQ missing required keys")
            return None
        if len(mcq["options"]) != 4:
            logger.error(f"MCQ has {len(mcq['options'])} options, expected 4")
            return None
        if not (0 <= int(mcq["answer_index"]) <= 3):
            logger.error("answer_index out of range")
            return None

        mcq["options"] = clean_options(mcq["options"])
        mcq["answer_index"] = int(mcq["answer_index"])

        if is_question_used(mcq["question"]):
            logger.warning("Duplicate question, retrying…")
            if retry < 2:
                await asyncio.sleep(5)
                return await generate_mcq(content, book_context, retry=retry + 1,
                                          auto_select_topic=auto_select_topic)
            logger.warning("Still duplicate after retries — using anyway")

        mark_question_used(mcq["question"])
        # Attach the internally-selected topic so callers can use it for labels
        if auto_select_topic and topic:
            mcq["_selected_topic"] = topic
        return mcq

    except Exception as e:
        logger.error(f"MCQ generation error: {e}")
        return None

# ======================
# REPHRASE FORWARDED MCQ
# ======================
async def rephrase_forwarded_mcq(text: str):
    prompt = (
        "You are a medical MCQ expert.\n\n"
        "Here is a forwarded MCQ:\n\n" + text + "\n\n"
        "Task:\n"
        "1. Slightly rephrase the question stem (keep same meaning)\n"
        "2. Keep the same options\n"
        "3. Identify the correct answer\n"
        "4. Add a detailed explanation\n\n"
        "Return ONLY this JSON (no markdown, no preamble):\n"
        '{"question": "rephrased question...", '
        '"options": ["option1", "option2", "option3", "option4"], '
        '"answer_index": 0, '
        '"explanation": "Correct: A because... B is wrong because..."}\n\n'
        "CRITICAL: options must be plain text only, no A) or A. prefix inside the options array."
    )
    try:
        response = await safe_groq_call(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
        )
        if not response:
            return None
        mcq = extract_json(response.choices[0].message.content)
        if mcq:
            mcq["options"] = clean_options(mcq.get("options", []))
            mcq["answer_index"] = int(mcq.get("answer_index", 0))
        return mcq
    except Exception as e:
        logger.error(f"Rephrase error: {e}")
        return None

# ======================
# IMAGE → MCQ
# ======================
async def generate_mcq_from_image(image_bytes: bytes, mime_type: str = "image/jpeg"):
    try:
        b64 = base64.b64encode(image_bytes).decode()
        vision = await safe_groq_call(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                    {"type": "text",
                     "text": "Extract all medical/biochemistry/pharmacology text from this image. Return raw text only."}
                ]
            }],
            temperature=0.1,
        )
        if not vision:
            return None, "Vision model unavailable"
        extracted = vision.choices[0].message.content
        if not extracted or len(extracted.strip()) < 20:
            return None, "Could not extract text from image"
        extracted = extracted[:4000]
        await asyncio.sleep(10)
        mcq = await generate_mcq(extracted)
        return mcq, extracted[:200]
    except Exception as e:
        logger.error(f"Image MCQ error: {e}")
        return None, str(e)

# ======================
# SEND FOR APPROVAL
# ======================
async def send_for_approval(bot, mcq: dict, source: str,
                             topic_content: str = None, book_context: str = None):
    """
    Persists the question to disk first, then sends the approval message.
    Uses short IDs (≤8 chars) so callback_data NEVER exceeds Telegram's 64-byte limit.
    """
    try:
        qid = make_short_id()
        # Guarantee uniqueness in the unlikely collision case
        while qid in pending_questions:
            qid = make_short_id()

        # Save to disk BEFORE sending (so it survives if bot restarts mid-flight)
        pending_questions[qid] = {
            "mcq":           mcq,
            "source":        source,
            "topic_content": topic_content,
            "book_context":  book_context,
        }
        save_pending()

        options_preview = []
        for i, opt in enumerate(mcq["options"]):
            marker = "✅ " if i == mcq["answer_index"] else ""
            options_preview.append(f"{marker}{chr(65+i)}. {opt}")

        # Keep approval message under Telegram's 4096-char limit
        explanation_preview = mcq["explanation"][:800]
        text = (
            "📋 NEW MCQ FOR APPROVAL\n\n"
            f"📚 Source: {source}\n\n"
            f"{mcq['question']}\n\n"
            + "\n".join(options_preview)
            + f"\n\n💡 Explanation:\n{explanation_preview}"
        )

        # callback_data = "approve_" (8) + qid (≤8) = ≤16 chars — well within 64
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve & Post", callback_data=f"ap_{qid}"),
            InlineKeyboardButton("❌ Reject",         callback_data=f"rj_{qid}"),
        ], [
            InlineKeyboardButton("🔄 Regenerate",     callback_data=f"rg_{qid}"),
        ]])

        await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=keyboard)
        logger.info(f"MCQ sent for approval (qid={qid}, source={source})")

    except Exception as e:
        logger.error(f"send_for_approval error: {e}")

# ======================
# POST TO CHANNEL
# ======================
async def post_to_channel(bot, mcq: dict):
    options_text = [f"{chr(65+i)}. {opt}" for i, opt in enumerate(mcq["options"])]
    text_msg = mcq["question"] + "\n\n" + "\n".join(options_text)

    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text_msg)
        await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"Failed to send question text: {e}")

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=mcq["question"][:300],
            options=[opt[:100] for opt in mcq["options"]],
            type="quiz",
            correct_option_id=mcq["answer_index"],
            is_anonymous=True,
        )
        await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"Failed to send poll: {e}")

    try:
        spoiler = "💡 *Explanation:*\n\n||" + escape_md(mcq["explanation"]) + "||"
        await bot.send_message(chat_id=CHANNEL_ID, text=spoiler, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Failed to send explanation (MarkdownV2): {e}")
        try:
            await bot.send_message(chat_id=CHANNEL_ID,
                                   text="💡 Explanation:\n\n" + mcq["explanation"])
        except Exception as e2:
            logger.error(f"Fallback explanation failed: {e2}")

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
            mcq = await generate_mcq(chunk, book_context="neet_pharma")
            if not mcq:
                logger.error("Failed to generate NEET Pharma MCQ")
                return
            await send_for_approval(context.bot, mcq, "NEET PG Pharmacology 2025",
                                    topic_content=chunk, book_context="neet_pharma")
        else:
            mcq = await generate_mcq("", book_context=subject, auto_select_topic=True)
            if not mcq:
                logger.error("Failed to generate MCQ")
                return
            topic = mcq.get("_selected_topic", subject)
            logger.info(f"Selected topic: {topic}")
            label = "Harper 33e" if subject == "harper" else "Goodman & Gilman"
            await send_for_approval(context.bot, mcq, f"{label}: {topic}",
                                    topic_content=topic, book_context=subject)
    except Exception as e:
        logger.error(f"Scheduled job error: {e}")

# ======================
# CALLBACK HANDLER
# ======================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g. "ap_abc123"

    # Parse prefix and qid  (prefixes are now 2–3 chars)
    if "_" not in data:
        await query.edit_message_text("❌ Unknown action.")
        return

    prefix, qid = data.split("_", 1)

    # ── APPROVE ──────────────────────────────────────────────
    if prefix == "ap":
        item = pending_questions.get(qid)
        if not item:
            await query.edit_message_text(
                "❌ Question not found.\n"
                "It may have already been approved/rejected, or the bot restarted.\n"
                "Use /postnow to generate a new one."
            )
            return
        await query.edit_message_text("⏳ Posting to channel…")
        await post_to_channel(context.bot, item["mcq"])
        pending_questions.pop(qid, None)
        save_pending()
        await context.bot.send_message(chat_id=ADMIN_ID, text="✅ Posted to channel!")

    # ── REJECT ───────────────────────────────────────────────
    elif prefix == "rj":
        pending_questions.pop(qid, None)
        save_pending()
        await query.edit_message_text("❌ Rejected and discarded.")

    # ── REGENERATE ───────────────────────────────────────────
    elif prefix == "rg":
        old_item = pending_questions.get(qid)
        if not old_item:
            await query.edit_message_text(
                "❌ Original question not found.\n"
                "It may have already been regenerated.\n"
                "Use /postnow to generate a fresh MCQ."
            )
            return

        topic_content = old_item.get("topic_content")
        book          = old_item.get("book_context")
        source        = old_item.get("source", "Unknown Source")

        if not topic_content:
            pending_questions.pop(qid, None)
            save_pending()
            await query.edit_message_text(
                "❌ No topic content stored for this question.\n"
                "Use /postnow to generate a fresh MCQ."
            )
            return

        # Remove old entry NOW so double-tapping the button does nothing harmful
        pending_questions.pop(qid, None)
        save_pending()

        await query.edit_message_text(
            f"🔄 Regenerating MCQ on same topic…\n📚 Source: {source}"
        )

        # Attempt up to 3 times
        mcq = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(10)
            mcq = await generate_mcq(topic_content, book_context=book)
            if mcq:
                break
            logger.warning(f"Regen attempt {attempt+1} failed")

        if mcq:
            await send_for_approval(context.bot, mcq, source,
                                    topic_content=topic_content, book_context=book)
        else:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "❌ Regeneration failed after 3 attempts (Groq API issue).\n"
                    f"Topic: {source}\n\n"
                    "Wait 1–2 min then try /postnow, /harper, or /goodman."
                )
            )

    else:
        await query.edit_message_text("❌ Unknown action.")

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
        text = (
            poll.question + "\n\n"
            + "\n".join(f"{chr(65+i)}) {opt.text}" for i, opt in enumerate(poll.options))
        )
        await update.message.reply_text("📊 Forwarded poll detected! Processing…")
        mcq = await rephrase_forwarded_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Could not process poll.")
            return
        await send_for_approval(context.bot, mcq, "Forwarded Poll",
                                topic_content=text, book_context=None)
    except Exception as e:
        logger.error(f"Forwarded poll error: {e}")
        await update.message.reply_text("❌ Failed to process poll.")

# ======================
# IMAGE HANDLER
# ======================
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("🖼️ Image received. Generating MCQ…")
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
        await send_for_approval(context.bot, mcq, "Image Upload",
                                topic_content=preview, book_context=None)
    except Exception as e:
        logger.error(f"Image handler error: {e}")
        await update.message.reply_text(f"❌ Image processing failed: {e}")

# ======================
# PDF HANDLER
# ======================
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        await update.message.reply_text("📄 PDF received. Extracting text…")
        file = await update.message.document.get_file()
        file_bytes = bytes(await file.download_as_bytearray())
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages[:10]:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
        if not text.strip():
            await update.message.reply_text("❌ Could not extract text from PDF.")
            return
        text = text[:4000]
        await update.message.reply_text("⏳ Generating MCQ from PDF…")
        mcq = await generate_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        await send_for_approval(context.bot, mcq, "PDF Upload",
                                topic_content=text, book_context=None)
    except Exception as e:
        logger.error(f"PDF handler error: {e}")
        await update.message.reply_text("❌ PDF processing failed.")

# ======================
# TEXT HANDLER
# ======================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = update.message.text.strip()

    if text.startswith("http://") or text.startswith("https://"):
        yt_id = extract_youtube_id(text)
        if yt_id:
            await update.message.reply_text("🎥 YouTube link! Fetching transcript…")
            content = await get_youtube_transcript(yt_id)
            if not content:
                await update.message.reply_text("⚠️ No transcript, trying page content…")
                content = await fetch_url_content(text)
            if not content:
                await update.message.reply_text("❌ Could not extract content.")
                return
            source = "YouTube: " + text[:50]
        else:
            await update.message.reply_text("🔗 Article URL! Fetching content…")
            content = await fetch_url_content(text)
            if not content:
                await update.message.reply_text("❌ Could not fetch content.")
                return
            source = "Article: " + text[:50]

        await update.message.reply_text("⏳ Generating MCQ…")
        mcq = await generate_mcq(content)
        if not mcq:
            await update.message.reply_text("❌ Failed to generate MCQ.")
            return
        await send_for_approval(context.bot, mcq, source,
                                topic_content=content, book_context=None)
    else:
        await update.message.reply_text("💬 Forwarded MCQ text detected! Processing…")
        mcq = await rephrase_forwarded_mcq(text)
        if not mcq:
            await update.message.reply_text("❌ Could not process MCQ.")
            return
        await send_for_approval(context.bot, mcq, "Forwarded MCQ",
                                topic_content=text, book_context=None)

# ======================
# COMMANDS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    logger.info(f"/start from user {uid}")
    if uid != ADMIN_ID:
        await update.message.reply_text(f"⛔ Unauthorized. Your ID: {uid}")
        return
    await update.message.reply_text(
        "✅ Pharma Quiz Bot Running!\n\n"
        "📚 Book Commands:\n"
        "/harper      — MCQ from Harper Biochemistry 33rd Ed\n"
        "/goodman     — MCQ from Goodman & Gilman\n"
        "/neetpharma  — MCQ from NEET PG Pharmacology 2025\n\n"
        "🤖 Other Commands:\n"
        "/postnow        — Generate next alternating MCQ\n"
        "/status         — Bot status\n"
        "/debug          — Debug info\n"
        "/resettopics    — Clear used topics\n"
        "/resetquestions — Clear used questions\n\n"
        "📎 Send anything:\n"
        "📝 Forwarded MCQ text  → rephrase & post\n"
        "📊 Forwarded MCQ poll  → rephrase & post\n"
        "📄 PDF                 → extract & generate MCQ\n"
        "🖼 Image               → analyze & generate MCQ\n"
        "🔗 Article URL         → scrape & generate MCQ\n"
        "🎥 YouTube URL         → transcript & generate MCQ\n\n"
        "🔄 Rotation: Biochem → Pharma → NEET Pharma → …\n"
        "🔄 Regenerate rephrases the SAME topic."
    )

async def harper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("📖 Generating MCQ from Harper's Biochemistry 33rd Edition…")
    mcq = await generate_mcq("", book_context="harper", auto_select_topic=True)
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    topic = mcq.get("_selected_topic", "Harper Biochemistry")
    await update.message.reply_text(f"🧬 Topic: {topic}")
    await send_for_approval(context.bot, mcq, f"Harper 33e: {topic}",
                            topic_content=topic, book_context="harper")

async def goodman_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("💊 Generating MCQ from Goodman & Gilman…")
    mcq = await generate_mcq("", book_context="goodman", auto_select_topic=True)
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    topic = mcq.get("_selected_topic", "Goodman & Gilman")
    await update.message.reply_text(f"💉 Topic: {topic}")
    await send_for_approval(context.bot, mcq, f"Goodman & Gilman: {topic}",
                            topic_content=topic, book_context="goodman")

async def neet_pharma_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not NEET_PHARMA_CHUNKS:
        await update.message.reply_text("❌ NEET Pharma PDF not loaded. Check books/neet_pharma.pdf.")
        return
    await update.message.reply_text(
        f"💊 Generating MCQ from NEET PG Pharmacology 2025…\n"
        f"📚 {len(NEET_PHARMA_CHUNKS)} chunks available"
    )
    chunk = await get_neet_pharma_chunk()
    mcq = await generate_mcq(chunk, book_context="neet_pharma")
    if not mcq:
        await update.message.reply_text("❌ Failed to generate MCQ.")
        return
    await send_for_approval(context.bot, mcq, "NEET PG Pharmacology 2025",
                            topic_content=chunk, book_context="neet_pharma")

async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Generating MCQ… please wait.")
    await scheduled_job(context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rotation = ["harper", "goodman", "neet_pharma"]
    current  = last_subject.get("subject", "goodman")
    try:
        nxt = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        nxt = "harper"
    await update.message.reply_text(
        "✅ Bot is running\n"
        f"📊 Pending approvals:  {len(pending_questions)}\n"
        f"📚 Topics used:        {len(used_topics)}\n"
        f"❓ Questions used:     {len(used_questions)}\n"
        f"🔄 Next subject:       {nxt}\n"
        f"🧬 Harper remaining:   {len([t for t in HARPER_TOPICS if t not in used_topics])}/40\n"
        f"💊 Goodman remaining:  {len([t for t in GOODMAN_TOPICS if t not in used_topics])}/40\n"
        f"📖 NEET Pharma:        {len(NEET_PHARMA_CHUNKS)} chunks, {len(used_neet_chunks)} used"
    )

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rotation = ["harper", "goodman", "neet_pharma"]
    current  = last_subject.get("subject", "goodman")
    try:
        nxt = rotation[(rotation.index(current) + 1) % len(rotation)]
    except ValueError:
        nxt = "harper"
    await update.message.reply_text(
        f"🔧 Debug Info\n"
        f"Your ID:           {uid}\n"
        f"Admin ID:          {ADMIN_ID}\n"
        f"Match:             {'✅' if uid == ADMIN_ID else '❌'}\n"
        f"Pending approvals: {len(pending_questions)}\n"
        f"Topics used:       {len(used_topics)}\n"
        f"Questions used:    {len(used_questions)}\n"
        f"Next subject:      {nxt}\n"
        f"Harper remaining:  {len([t for t in HARPER_TOPICS if t not in used_topics])}/40\n"
        f"Goodman remaining: {len([t for t in GOODMAN_TOPICS if t not in used_topics])}/40\n"
        f"NEET chunks:       {len(NEET_PHARMA_CHUNKS)} loaded, {len(used_neet_chunks)} used"
    )

async def reset_topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    used_topics.clear()
    save_json("used_topics.json", used_topics)
    await update.message.reply_text("✅ Used topics cleared!")

async def reset_questions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    used_questions.clear()
    save_json("used_questions.json", used_questions)
    await update.message.reply_text("✅ Used questions cleared!")

# ======================
# MAIN
# ======================
def main():
    global NEET_PHARMA_CHUNKS
    NEET_PHARMA_CHUNKS = load_pdf_chunks("books/neet_pharma.pdf")
    logger.info(f"NEET Pharma chunks: {len(NEET_PHARMA_CHUNKS)}")
    logger.info(f"Pending questions restored: {len(pending_questions)}")
    logger.info("Starting Pharma Quiz Bot…")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("postnow",        post_now))
    app.add_handler(CommandHandler("status",         status))
    app.add_handler(CommandHandler("harper",         harper_command))
    app.add_handler(CommandHandler("goodman",        goodman_command))
    app.add_handler(CommandHandler("neetpharma",     neet_pharma_command))
    app.add_handler(CommandHandler("debug",          debug_command))
    app.add_handler(CommandHandler("resettopics",    reset_topics_command))
    app.add_handler(CommandHandler("resetquestions", reset_questions_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.PDF,              handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO,                     handle_image))
    app.add_handler(MessageHandler(filters.Document.IMAGE,            handle_image))
    app.add_handler(MessageHandler(filters.POLL,                      handle_forwarded_poll))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,   handle_text))

    app.job_queue.run_repeating(scheduled_job, interval=INTERVAL, first=30)

    logger.info(f"Bot started! Interval: {INTERVAL // 60} minutes")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
