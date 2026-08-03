import asyncio, json, os, re, hashlib
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from bs4 import BeautifulSoup
from bloom_filter2 import BloomFilter
from fastapi import FastAPI
import sys

BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
bf = BloomFilter(max_elements=1_000_000, error_rate=0.001)

# Common crypto ticker mappings (full name -> ticker)
CRYPTO_MAPPINGS = {
    "bitcoin": "BTC",
    "ethereum": "ETH", 
    "cardano": "ADA",
    "solana": "SOL",
    "polkadot": "DOT",
    "chainlink": "LINK",
    "polygon": "MATIC",
    "avalanche": "AVAX",
    "cosmos": "ATOM",
    "terra": "LUNA",
    "uniswap": "UNI",
    "sushiswap": "SUSHI",
    "pancakeswap": "CAKE",
}

# Known crypto tickers (to avoid false positives)
KNOWN_TICKERS = {
    "BTC", "ETH", "SOL", "ADA", "DOT", "LINK", "MATIC", "AVAX", "ATOM", 
    "LUNA", "UNI", "SUSHI", "CAKE", "BNB", "XRP", "DOGE", "SHIB", "LTC",
    "BCH", "XLM", "VET", "TRX", "FIL", "ICP", "NEAR", "FTM", "ALGO", "HBAR"
}

def extract_crypto_tickers(text: str) -> list:
    """Extract cryptocurrency tickers from text with improved detection."""
    tickers = []
    
    # Pattern 1: $BTC, $ETH, etc.
    dollar_pattern = re.compile(r"\$([A-Z]{2,5})\b")
    dollar_matches = dollar_pattern.findall(text)
    tickers.extend(dollar_matches)
    
    # Pattern 2: Full names like Bitcoin, Ethereum
    for full_name, ticker in CRYPTO_MAPPINGS.items():
        if re.search(rf"\b{full_name}\b", text, re.IGNORECASE):
            tickers.append(ticker)
    
    # Pattern 3: Standalone tickers (only known ones to avoid false positives)
    words = re.findall(r"\b[A-Z]{2,5}\b", text)
    for word in words:
        if word in KNOWN_TICKERS and word not in tickers:
            tickers.append(word)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_tickers = []
    for ticker in tickers:
        if ticker not in seen:
            seen.add(ticker)
            unique_tickers.append(ticker)
    
    return unique_tickers

app = FastAPI()

@app.get("/healthz")
def health():
    return {"status": "ok"}

def strip_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)

async def pipe():
    consumer = AIOKafkaConsumer(
        "raw_news",
        bootstrap_servers=BROKER,
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=BROKER,
        value_serializer=lambda v: json.dumps(v).encode(),
    )
    await consumer.start(), await producer.start()
    try:
        async for msg in consumer:
            if msg.value["id"] in bf:
                continue
            bf.add(msg.value["id"])
            
            # Process both title and content if available
            title = strip_html(msg.value.get("title", ""))
            content = strip_html(msg.value.get("content", ""))
            full_text = f"{title} {content}".strip()
            
            # Extract tickers from full text
            tickers = extract_crypto_tickers(full_text)
            
            msg.value.update({
                "text": full_text,
                "title_clean": title,
                "tickers": tickers,
                "ticker_count": len(tickers),
            })
            
            await producer.send_and_wait("clean_news", msg.value)
    finally:
        await consumer.stop(), await producer.stop()

# Test function to verify ticker extraction works
def test_ticker_extraction():
    """Test the ticker extraction function."""
    test_cases = [
        "Bitcoin and Ethereum surge as SOL reaches new highs",
        "Cardano also showing strong performance",
        "$BTC and $ETH are leading the market",
        "The price of Bitcoin and SOL is increasing",
        "This is a normal sentence without crypto tickers",
    ]
    
    for test_text in test_cases:
        tickers = extract_crypto_tickers(test_text)
        print(f"Text: {test_text}")
        print(f"Tickers found: {tickers}")
        print("-" * 50)

if __name__ == "__main__":
    # Run test if called directly
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_ticker_extraction()
    else:
        asyncio.run(pipe())
