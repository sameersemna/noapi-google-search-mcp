# LM Studio Integration Test Report

**Date:** 2026-07-10 15:14:13

**Model:** llama/qwen-1.5b

**MCP Server:** noapi-google-search-mcp v0.3.0 (38 tools)

**Result:** 38/42 passed, 4 failed, 0 errors

**Total time:** 329.6s | **Avg latency:** 7.8s/call

---

## Summary Table

| # | Category | Prompt | Expected | Called | Args OK | Status | Latency |
|---|----------|--------|----------|--------|---------|--------|---------|
| 1 | Google Search | Search Google for best budget GPU for LL... | `google_search` | `google_search` | yes | PASS | 78.8s |
| 2 | Google News | Find recent news about OpenAI | `google_news` | `google_news` | yes | PASS | 6.2s |
| 3 | Google Scholar | Find academic papers about attention mec... | `google_scholar` | `google_scholar` | yes | PASS | 6.5s |
| 4 | Google Images | Show me images of the Northern Lights | `google_images` | `google_images` | yes | PASS | 6.0s |
| 5 | Google Weather | What is the weather in Tokyo right now? | `google_weather` | `google_weather` | yes | PASS | 4.9s |
| 6 | Google Finance | What is Apple stock price? Use google_fi... | `google_finance` | `google_finance` | no | PARTIAL | 5.2s |
| 7 | Google Translate | Translate 'good morning' to German | `google_translate` | `google_translate` | yes | PASS | 5.9s |
| 8 | Google Shopping | Find me the cheapest RTX 4090 for sale | `google_shopping` | `google_shopping` | yes | PASS | 6.7s |
| 9 | Google Flights | Search for flights from New York to Lond... | `google_flights` | `google_flights` | yes | PASS | 5.8s |
| 10 | Google Hotels | Search for hotels in Paris | `google_hotels` | `google_hotels` | yes | PASS | 8.1s |
| 11 | Google Maps | Use google_maps to find pizza restaurant... | `google_maps` | `google_maps` | yes | PASS | 6.1s |
| 12 | Google Maps Directions | Get driving directions from Berlin to Mu... | `google_maps_directions` | `google_maps_directions` | yes | PASS | 7.2s |
| 13 | Google Trends | Show me Google Trends for artificial int... | `google_trends` | `google_trends` | yes | PASS | 5.3s |
| 14 | Google Books | Find books about machine learning | `google_books` | `google_books` | yes | PASS | 6.1s |
| 15 | Visit Page | Read this web page for me: https://examp... | `visit_page` | `visit_page` | yes | PASS | 5.2s |
| 16 | Google Lens | Use Google Lens reverse image search on ... | `google_lens` | `google_lens` | yes | PASS | 5.4s |
| 17 | OCR Image | Extract text from this screenshot using ... | `ocr_image` | `ocr_image` | yes | PASS | 5.7s |
| 18 | Transcribe Video | Transcribe this YouTube video: https://y... | `transcribe_video` | `transcribe_video` | yes | PASS | 6.9s |
| 19 | Search Transcript | Search the transcript of https://youtube... | `search_transcript` | `search_transcript` | yes | PASS | 8.2s |
| 20 | Extract Video Clip | Extract a clip from https://youtube.com/... | `extract_video_clip` | `extract_video_clip` | yes | PASS | 9.4s |
| 21 | Subscribe (News) | Subscribe to BBC News feed | `subscribe` | `subscribe` | yes | PASS | 6.1s |
| 22 | Subscribe (Reddit) | Subscribe to the subreddit r/LocalLLaMA | `subscribe` | `subscribe` | yes | PASS | 6.5s |
| 23 | Subscribe (HN) | Subscribe to Hacker News top stories | `subscribe` | `subscribe` | no | PARTIAL | 6.1s |
| 24 | Subscribe (YouTube) | Subscribe to the YouTube channel @3Blue1... | `subscribe` | `subscribe` | yes | PASS | 6.3s |
| 25 | Subscribe (GitHub) | Watch the GitHub repo anthropics/claude-... | `subscribe` | `subscribe` | yes | PASS | 6.9s |
| 26 | Subscribe (arXiv) | Subscribe to the machine learning arXiv ... | `subscribe` | `subscribe` | yes | PASS | 6.2s |
| 27 | Subscribe (Twitter) | Follow @elonmusk on Twitter | `subscribe` | `subscribe` | yes | PASS | 6.2s |
| 28 | List Subscriptions | Show me all my feed subscriptions | `list_subscriptions` | `list_subscriptions` | yes | PASS | 4.6s |
| 29 | Check Feeds | Check all my feeds for new content | `check_feeds` | `check_feeds` | yes | PASS | 5.2s |
| 30 | Search Feeds | Search my feeds for transformer architec... | `search_feeds` | `check_feeds` | no | FAIL | 5.3s |
| 31 | Get Feed Items | Show me the latest items from my Reddit ... | `get_feed_items` | `check_feeds` | yes | FAIL | 5.5s |
| 32 | Unsubscribe | Unsubscribe from BBC News | `unsubscribe` | `unsubscribe` | yes | PASS | 5.8s |
| 33 | Transcribe Local | Transcribe this local recording: ~/meeti... | `transcribe_local` | `transcribe_local` | yes | PASS | 5.8s |
| 34 | Convert Media | Convert video.mp4 to mp3 format | `convert_media` | `convert_media` | yes | PASS | 6.4s |
| 35 | Read Document | Read this PDF document: ~/report.pdf | `read_document` | `read_document` | yes | PASS | 5.2s |
| 36 | Fetch Emails | Check my email at user@gmail.com with pa... | `fetch_emails` | `fetch_emails` | yes | PASS | 6.7s |
| 37 | Shorten URL | Shorten this URL: https://www.example.co... | `shorten_url` | `shorten_url` | yes | PASS | 6.5s |
| 38 | Wikipedia | Look up quantum computing on Wikipedia | `wikipedia` | `wikipedia` | yes | PASS | 5.3s |
| 39 | Paste Text | Post this text to a pastebin: Hello Worl... | `paste_text` | `paste_text` | yes | PASS | 5.5s |
| 40 | Generate QR | Generate a QR code for https://mysite.co... | `generate_qr` | `generate_qr` | yes | PASS | 5.4s |
| 41 | Archive Webpage | Archive this webpage on the Wayback Mach... | `archive_webpage` | `archive_webpage` | yes | PASS | 5.6s |
| 42 | Upload to S3 | Upload report.pdf to my S3 bucket called... | `upload_to_s3` | `upload_to_s3` | yes | PASS | 6.8s |

---

## Results by Category

| Category | Passed | Failed | Total |
|----------|--------|--------|-------|
| Google Search | 1 | 0 | 1 |
| Google News | 1 | 0 | 1 |
| Google Scholar | 1 | 0 | 1 |
| Google Images | 1 | 0 | 1 |
| Google Weather | 1 | 0 | 1 |
| Google Finance | 0 | 1 | 1 |
| Google Translate | 1 | 0 | 1 |
| Google Shopping | 1 | 0 | 1 |
| Google Flights | 1 | 0 | 1 |
| Google Hotels | 1 | 0 | 1 |
| Google Maps | 1 | 0 | 1 |
| Google Maps Directions | 1 | 0 | 1 |
| Google Trends | 1 | 0 | 1 |
| Google Books | 1 | 0 | 1 |
| Visit Page | 1 | 0 | 1 |
| Google Lens | 1 | 0 | 1 |
| OCR Image | 1 | 0 | 1 |
| Transcribe Video | 1 | 0 | 1 |
| Search Transcript | 1 | 0 | 1 |
| Extract Video Clip | 1 | 0 | 1 |
| Subscribe (News) | 1 | 0 | 1 |
| Subscribe (Reddit) | 1 | 0 | 1 |
| Subscribe (HN) | 0 | 1 | 1 |
| Subscribe (YouTube) | 1 | 0 | 1 |
| Subscribe (GitHub) | 1 | 0 | 1 |
| Subscribe (arXiv) | 1 | 0 | 1 |
| Subscribe (Twitter) | 1 | 0 | 1 |
| List Subscriptions | 1 | 0 | 1 |
| Check Feeds | 1 | 0 | 1 |
| Search Feeds | 0 | 1 | 1 |
| Get Feed Items | 0 | 1 | 1 |
| Unsubscribe | 1 | 0 | 1 |
| Transcribe Local | 1 | 0 | 1 |
| Convert Media | 1 | 0 | 1 |
| Read Document | 1 | 0 | 1 |
| Fetch Emails | 1 | 0 | 1 |
| Shorten URL | 1 | 0 | 1 |
| Wikipedia | 1 | 0 | 1 |
| Paste Text | 1 | 0 | 1 |
| Generate QR | 1 | 0 | 1 |
| Archive Webpage | 1 | 0 | 1 |
| Upload to S3 | 1 | 0 | 1 |

---

## Detailed Input/Output Log

### Test 01: Google Search - PASS

**User prompt:**
```
Search Google for best budget GPU for LLM inference
```

**Expected tool:** `google_search`

**Tool called:** `google_search`

**Arguments:**
```json
{
  "query": "best budget GPU for LLM inference",
  "num_results": 10
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 78.8s

### Test 02: Google News - PASS

**User prompt:**
```
Find recent news about OpenAI
```

**Expected tool:** `google_news`

**Tool called:** `google_news`

**Arguments:**
```json
{
  "query": "OpenAI",
  "num_results": 5
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.2s

### Test 03: Google Scholar - PASS

**User prompt:**
```
Find academic papers about attention mechanisms in transformers
```

**Expected tool:** `google_scholar`

**Tool called:** `google_scholar`

**Arguments:**
```json
{
  "query": "attention mechanisms in transformers",
  "num_results": 5
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.5s

### Test 04: Google Images - PASS

**User prompt:**
```
Show me images of the Northern Lights
```

**Expected tool:** `google_images`

**Tool called:** `google_images`

**Arguments:**
```json
{
  "query": "Northern Lights",
  "num_results": 5
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.0s

### Test 05: Google Weather - PASS

**User prompt:**
```
What is the weather in Tokyo right now?
```

**Expected tool:** `google_weather`

**Tool called:** `google_weather`

**Arguments:**
```json
{
  "location": "Tokyo"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 4.9s

### Test 06: Google Finance - PARTIAL

**User prompt:**
```
What is Apple stock price? Use google_finance to look it up.
```

**Expected tool:** `google_finance`

**Tool called:** `google_finance`

**Arguments:**
```json
{
  "query": "AAPL"
}
```

**Tool correct:** True | **Args correct:** False | **Latency:** 5.2s

### Test 07: Google Translate - PASS

**User prompt:**
```
Translate 'good morning' to German
```

**Expected tool:** `google_translate`

**Tool called:** `google_translate`

**Arguments:**
```json
{
  "text": "good morning",
  "to_language": "de"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.9s

### Test 08: Google Shopping - PASS

**User prompt:**
```
Find me the cheapest RTX 4090 for sale
```

**Expected tool:** `google_shopping`

**Tool called:** `google_shopping`

**Arguments:**
```json
{
  "query": "RTX 4090",
  "num_results": 5
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.7s

### Test 09: Google Flights - PASS

**User prompt:**
```
Search for flights from New York to London
```

**Expected tool:** `google_flights`

**Tool called:** `google_flights`

**Arguments:**
```json
{
  "origin": "New York",
  "destination": "London"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.8s

### Test 10: Google Hotels - PASS

**User prompt:**
```
Search for hotels in Paris
```

**Expected tool:** `google_hotels`

**Tool called:** `google_hotels`

**Arguments:**
```json
{
  "query": "hotels in Paris",
  "num_results": 10
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 8.1s

### Test 11: Google Maps - PASS

**User prompt:**
```
Use google_maps to find pizza restaurants near Times Square in New York
```

**Expected tool:** `google_maps`

**Tool called:** `google_maps`

**Arguments:**
```json
{
  "query": "pizza restaurants near Times Square, New York"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.1s

### Test 12: Google Maps Directions - PASS

**User prompt:**
```
Get driving directions from Berlin to Munich using google_maps_directions
```

**Expected tool:** `google_maps_directions`

**Tool called:** `google_maps_directions`

**Arguments:**
```json
{
  "origin": "Berlin, Germany",
  "destination": "Munich, Germany"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 7.2s

### Test 13: Google Trends - PASS

**User prompt:**
```
Show me Google Trends for artificial intelligence
```

**Expected tool:** `google_trends`

**Tool called:** `google_trends`

**Arguments:**
```json
{
  "query": "artificial intelligence"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.3s

### Test 14: Google Books - PASS

**User prompt:**
```
Find books about machine learning
```

**Expected tool:** `google_books`

**Tool called:** `google_books`

**Arguments:**
```json
{
  "query": "machine learning",
  "num_results": 10
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.1s

### Test 15: Visit Page - PASS

**User prompt:**
```
Read this web page for me: https://example.com
```

**Expected tool:** `visit_page`

**Tool called:** `visit_page`

**Arguments:**
```json
{
  "url": "https://example.com"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.2s

### Test 16: Google Lens - PASS

**User prompt:**
```
Use Google Lens reverse image search on this image: /tmp/test.jpg
```

**Expected tool:** `google_lens`

**Tool called:** `google_lens`

**Arguments:**
```json
{
  "image_source": "/tmp/test.jpg"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.4s

### Test 17: OCR Image - PASS

**User prompt:**
```
Extract text from this screenshot using OCR: /tmp/screenshot.png
```

**Expected tool:** `ocr_image`

**Tool called:** `ocr_image`

**Arguments:**
```json
{
  "image_source": "/tmp/screenshot.png"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.7s

### Test 18: Transcribe Video - PASS

**User prompt:**
```
Transcribe this YouTube video: https://youtube.com/watch?v=abc123
```

**Expected tool:** `transcribe_video`

**Tool called:** `transcribe_video`

**Arguments:**
```json
{
  "url": "https://youtube.com/watch?v=abc123"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.9s

### Test 19: Search Transcript - PASS

**User prompt:**
```
Search the transcript of https://youtube.com/watch?v=abc123 for the word 'attention'
```

**Expected tool:** `search_transcript`

**Tool called:** `search_transcript`

**Arguments:**
```json
{
  "url": "https://youtube.com/watch?v=abc123",
  "query": "attention"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 8.2s

### Test 20: Extract Video Clip - PASS

**User prompt:**
```
Extract a clip from https://youtube.com/watch?v=abc123 from 60 to 120 seconds
```

**Expected tool:** `extract_video_clip`

**Tool called:** `extract_video_clip`

**Arguments:**
```json
{
  "url": "https://youtube.com/watch?v=abc123",
  "start_seconds": 60,
  "end_seconds": 120
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 9.4s

### Test 21: Subscribe (News) - PASS

**User prompt:**
```
Subscribe to BBC News feed
```

**Expected tool:** `subscribe`

**Tool called:** `subscribe`

**Arguments:**
```json
{
  "source_type": "news",
  "identifier": "BBC News"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.1s

### Test 22: Subscribe (Reddit) - PASS

**User prompt:**
```
Subscribe to the subreddit r/LocalLLaMA
```

**Expected tool:** `subscribe`

**Tool called:** `subscribe`

**Arguments:**
```json
{
  "source_type": "reddit",
  "identifier": "r/LocalLLaMA"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.5s

### Test 23: Subscribe (HN) - PARTIAL

**User prompt:**
```
Subscribe to Hacker News top stories
```

**Expected tool:** `subscribe`

**Tool called:** `subscribe`

**Arguments:**
```json
{
  "source_type": "reddit",
  "identifier": "HackerNews"
}
```

**Tool correct:** True | **Args correct:** False | **Latency:** 6.1s

### Test 24: Subscribe (YouTube) - PASS

**User prompt:**
```
Subscribe to the YouTube channel @3Blue1Brown
```

**Expected tool:** `subscribe`

**Tool called:** `subscribe`

**Arguments:**
```json
{
  "source_type": "youtube",
  "identifier": "@3Blue1Brown"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.3s

### Test 25: Subscribe (GitHub) - PASS

**User prompt:**
```
Watch the GitHub repo anthropics/claude-code for new releases
```

**Expected tool:** `subscribe`

**Tool called:** `subscribe`

**Arguments:**
```json
{
  "source_type": "github",
  "identifier": "anthropics/claude-code"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.9s

### Test 26: Subscribe (arXiv) - PASS

**User prompt:**
```
Subscribe to the machine learning arXiv category
```

**Expected tool:** `subscribe`

**Tool called:** `subscribe`

**Arguments:**
```json
{
  "source_type": "arxiv",
  "identifier": "machine-learning"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.2s

### Test 27: Subscribe (Twitter) - PASS

**User prompt:**
```
Follow @elonmusk on Twitter
```

**Expected tool:** `subscribe`

**Tool called:** `subscribe`

**Arguments:**
```json
{
  "source_type": "twitter",
  "identifier": "elonmusk"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.2s

### Test 28: List Subscriptions - PASS

**User prompt:**
```
Show me all my feed subscriptions
```

**Expected tool:** `list_subscriptions`

**Tool called:** `list_subscriptions`

**Arguments:**
```json
{}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 4.6s

### Test 29: Check Feeds - PASS

**User prompt:**
```
Check all my feeds for new content
```

**Expected tool:** `check_feeds`

**Tool called:** `check_feeds`

**Arguments:**
```json
{
  "source_type": "youtube"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.2s

### Test 30: Search Feeds - FAIL

**User prompt:**
```
Search my feeds for transformer architecture
```

**Expected tool:** `search_feeds`

**Tool called:** `check_feeds`

**Arguments:**
```json
{
  "source_type": "youtube"
}
```

**Tool correct:** False | **Args correct:** False | **Latency:** 5.3s

### Test 31: Get Feed Items - FAIL

**User prompt:**
```
Show me the latest items from my Reddit feeds
```

**Expected tool:** `get_feed_items`

**Tool called:** `check_feeds`

**Arguments:**
```json
{
  "source_type": "reddit"
}
```

**Tool correct:** False | **Args correct:** True | **Latency:** 5.5s

### Test 32: Unsubscribe - PASS

**User prompt:**
```
Unsubscribe from BBC News
```

**Expected tool:** `unsubscribe`

**Tool called:** `unsubscribe`

**Arguments:**
```json
{
  "source_type": "news",
  "identifier": "BBC News"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.8s

### Test 33: Transcribe Local - PASS

**User prompt:**
```
Transcribe this local recording: ~/meeting.mp3
```

**Expected tool:** `transcribe_local`

**Tool called:** `transcribe_local`

**Arguments:**
```json
{
  "file_path": "/home/user/meeting.mp3"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.8s

### Test 34: Convert Media - PASS

**User prompt:**
```
Convert video.mp4 to mp3 format
```

**Expected tool:** `convert_media`

**Tool called:** `convert_media`

**Arguments:**
```json
{
  "input_path": "video.mp4",
  "output_format": "mp3"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.4s

### Test 35: Read Document - PASS

**User prompt:**
```
Read this PDF document: ~/report.pdf
```

**Expected tool:** `read_document`

**Tool called:** `read_document`

**Arguments:**
```json
{
  "file_path": "/report.pdf"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.2s

### Test 36: Fetch Emails - PASS

**User prompt:**
```
Check my email at user@gmail.com with password abc123
```

**Expected tool:** `fetch_emails`

**Tool called:** `fetch_emails`

**Arguments:**
```json
{
  "email": "user@gmail.com",
  "password": "abc123"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.7s

### Test 37: Shorten URL - PASS

**User prompt:**
```
Shorten this URL: https://www.example.com/very/long/path
```

**Expected tool:** `shorten_url`

**Tool called:** `shorten_url`

**Arguments:**
```json
{
  "url": "https://www.example.com/very/long/path"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.5s

### Test 38: Wikipedia - PASS

**User prompt:**
```
Look up quantum computing on Wikipedia
```

**Expected tool:** `wikipedia`

**Tool called:** `wikipedia`

**Arguments:**
```json
{
  "query": "quantum computing"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.3s

### Test 39: Paste Text - PASS

**User prompt:**
```
Post this text to a pastebin: Hello World test paste
```

**Expected tool:** `paste_text`

**Tool called:** `paste_text`

**Arguments:**
```json
{
  "content": "Hello World test paste"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.5s

### Test 40: Generate QR - PASS

**User prompt:**
```
Generate a QR code for https://mysite.com
```

**Expected tool:** `generate_qr`

**Tool called:** `generate_qr`

**Arguments:**
```json
{
  "data": "https://mysite.com"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.4s

### Test 41: Archive Webpage - PASS

**User prompt:**
```
Archive this webpage on the Wayback Machine: https://example.com
```

**Expected tool:** `archive_webpage`

**Tool called:** `archive_webpage`

**Arguments:**
```json
{
  "url": "https://example.com"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 5.6s

### Test 42: Upload to S3 - PASS

**User prompt:**
```
Upload report.pdf to my S3 bucket called my-docs
```

**Expected tool:** `upload_to_s3`

**Tool called:** `upload_to_s3`

**Arguments:**
```json
{
  "file_path": "report.pdf",
  "bucket": "my-docs"
}
```

**Tool correct:** True | **Args correct:** True | **Latency:** 6.8s

---

## Failures

- **Test 6** (Google Finance): PARTIAL - wrong args: {"query": "AAPL"}
- **Test 23** (Subscribe (HN)): PARTIAL - wrong args: {"source_type": "reddit", "identifier": "HackerNews"}
- **Test 30** (Search Feeds): FAIL - called `check_feeds` instead of `search_feeds`
- **Test 31** (Get Feed Items): FAIL - called `check_feeds` instead of `get_feed_items`
