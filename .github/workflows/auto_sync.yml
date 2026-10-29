name: Topsurge Playlist Auto Update

on:
  workflow_dispatch:
  # Render থেকে সিগন্যাল পাওয়ার ট্রিগার
  repository_dispatch:
    types: [source_updated]
  # ব্যাকআপ হিসেবে গিটহাবের নিজস্ব ১০ মিনিটের শিডিউল
  schedule:
    - cron: "*/10 * * * *"

permissions:
  contents: write

jobs:
  update:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          pip install "httpx[http2]"

      - name: Run Scraper
        run: |
          python files/stc.py

      - name: Commit and push changes
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

          git add strmcntr.json strmcntr.m3u logs/

          if git diff --cached --quiet; then
            echo "No changes to commit."
            exit 0
          fi

          git commit -m "Auto: Update LIVE matches and streams"
          git push
