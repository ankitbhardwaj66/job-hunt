# LinkedIn Prospector

Automated tool to find small tech companies, identify decision-makers, and send personalized LinkedIn connection requests.

## Setup

```bash
# Clone
git clone git@github.com:ankitbhardwaj66/job-hunt.git
cd job-hunt

# Run (creates venv, installs deps on first run)
./run.sh --login   # First time: opens browser for manual LinkedIn login
```

**Required env var:**
```bash
export ANTHROPIC_API_KEY=your_key_here
```

**Google Sheets setup:**
1. Share your Google Sheet with `stock-screener@xirrledger.iam.gserviceaccount.com` (Editor)
2. Update `sheet_url` in `config.json`

## Usage

```bash
./run.sh                    # Search only, no connection requests
./run.sh --connect          # Search + auto-send connection requests
./run.sh --local            # Chandigarh mode (local companies only)
./run.sh --local --connect  # Chandigarh mode + auto-send
./run.sh --login            # Re-login if session expired
```

## How It Works

### Global Mode (`./run.sh --connect`)

**Target:** Small tech companies (11-50 employees) worldwide.

1. **Search** LinkedIn using faceted filters — no keywords. Filters set in `config.json`:
   - `industry_codes`: IT Services, Software Development, Tech/Internet, Cloud, Security, etc.
   - `company_size_codes`: `["C"]` = 11–50 employees
   - Paginates up to 10 pages of results per run
2. **Filter out** spammy/generic companies:
   - Companies with country names ("OrpinsAI USA")
   - Incubators, academies, VC firms, communities, events
   - Companies in `skip_companies` list (e.g. past employers)
   - Already prospected companies (checked against Google Sheet)
3. **Find decision-makers** at each company, sorted by priority:
   - CTO / Chief Technology Officer
   - Engineering Manager / Tech Lead
   - Head of Engineering / VP of Engineering
   - Director of Engineering
   - COO / CMO / Managing Director
   - CEO
   - Founder / Co-Founder
4. **Filter out non-targets:**
   - Individual contributors (developers, engineers, architects)
   - Mid-level managers (project managers, scrum masters)
   - Job seekers (Open to Work badge, "available for" in headline)
   - Trainers, recruiters, designers, freelancers
5. **Check activity** on their profile — must have 2+ activities (posts, comments, reactions) in last **60 days**
6. **Generate message** using Claude AI:
   - Casual, human-sounding, under 300 characters
   - Uses short company name, not full legal name
   - Checks if headline matches company (detects job changes)
   - Randomly rotates skill highlights (Python/backend, DevOps, AWS/K8s)
   - No emojis, no corporate speak
7. **Send connection request** with personalized note:
   - Matches Connect button by person's name (avoids sidebar clicks)
   - Tries main button first, then More dropdown
   - Retries "Add a note" button up to 3-5 times
   - Logs `sent_no_note` if LinkedIn sends without note option
8. **Save to Google Sheet** with clickable links on name and company

### Local Mode (`./run.sh --local --connect`)

**Target:** Companies headquartered in Chandigarh (11-50 employees).

Everything from global mode, plus:

- **Location filter** uses LinkedIn's `companyHqGeo` parameter (Chandigarh geo ID: `104458930`)
- **Activity window** is **60 days** (smaller local pool)
- **Detects person's location** from their profile before messaging
- **Chandigarh tricity** — Mohali, SAS Nagar, Panchkula, Zirakpur, Kharar, Derabassi, Baddi all count as local
- **Message adapts to location:**
  - Person in Chandigarh/tricity: "Hey, fellow Chandigarh person here! ..."
  - Person elsewhere (e.g. CEO in England): "Hey, I'm a remote engineer based in India ..." (no false locality claim)
- Local prospects marked with `local: yes` in Google Sheet

### What Gets Saved to Google Sheet

| Column | Description |
|---|---|
| name | Clickable link to LinkedIn profile |
| company | Clickable link to company page |
| matched_role | Role that matched (cto, founder, etc.) |
| has_recent_activity | True if 2+ activities in window |
| recent_activity_30d | Number of activities found |
| connection_degree | 1st, 2nd, 3rd |
| found_date | Date prospected |
| connect_sent | True / sent_no_note / False |
| local | yes / no |

Companies with no decision-makers get a `no_contact_found` row so they're skipped on next run.

## Config

Edit `config.json`:

```json
{
  "industry_codes": ["96", "4", "6", "3", "48", "5", "2458"],
  "company_size_codes": ["C"],
  "max_companies_per_run": 15,
  "max_connects_per_company": 2,
  "skip_companies": ["Netsmartz"],
  "delay_between_actions": { "min_seconds": 3, "max_seconds": 8 },
  "local_mode": {
    "location": "Chandigarh",
    "geo_id": "104458930"
  }
}
```

### Industry Codes Reference

| Code | Industry |
|------|----------|
| `96` | IT Services and IT Consulting |
| `4`  | Software Development |
| `6`  | Technology, Information and Internet |
| `3`  | Technology, Information and Media |
| `48` | Computer and Network Security |
| `5`  | Computer Networking |
| `2458` | Data Infrastructure and Analytics |

To add more: go to LinkedIn company search → Industry filter → select the industry → copy the code from the URL's `industryCompanyVertical` parameter.

### Company Size Codes

| Code | Size |
|------|------|
| `B`  | 1–10 |
| `C`  | 11–50 |
| `D`  | 51–200 |

## Safety

- Random delays between actions (3-12s)
- Real Chrome browser (not headless)
- Hides automation flags
- Session persisted locally (no credentials in code)
- Pagination up to 10 pages per run
- Max 2 connects per company
- Never sends connect without trying to add a note first
