# Open Source Trading Analytics Suite 📊

## 💡 Overview / 專案簡介
A professional, standalone Python tool designed to parse, aggregate, and analyze exported broker statements (specifically Charles Schwab and ThinkorSwim) to generate an interactive, web-based Day Trading dashboard. 

This project transforms raw CSV export data into a **fully automated, institutional-grade analytics suite**, featuring a 90-Day Discipline Challenge tracker, Equity Curve Monte Carlo simulations, and AI-driven insights—all running locally without external server dependencies.

## 🔥 Key Features / 核心功能
* **Robust Broker Parsing (Charles Schwab / TOS):** Automatically detects and cleans trade executions, handles partial fills, calculates commissions, and builds round-trip trade data.
* **90-Day Challenge Engine:** Tracks process-oriented goals (e.g., maximum drawdown, consistency rule, and daily discipline strikes) across a 3-stage professional scaling roadmap.
* **Monte Carlo Equity Simulator:** Projects 20 randomized future equity paths over a 12-month horizon based on real historical win rates, R-multiples, and trade frequency.
* **Local Web Dashboard (HTML/JS/Chart.js):** Generates a zero-dependency, highly responsive `index.html` report with interactive radar charts, session analysis (Time of Day), and per-share profit distribution scatter plots.
* **Integrated Journaling (IndexedDB):** Allows pasting and saving chart screenshots directly within the browser (no cloud storage needed) alongside daily "Process Goal" evaluations.

## 🛠️ Tech Stack / 技術棧
* **Backend logic & parsing:** Python (pandas, re, datetime, json)
* **Frontend visualization:** HTML5, CSS3, JavaScript (Chart.js)
* **Local Storage:** IndexedDB (for screenshot persistence), LocalStorage (for user settings)
* **Architecture:** Python script parses data -> Injects JSON payload into a static HTML template -> Opens locally in the default browser.

## 📈 Why I Built This / 開發動機與業務價值
Standard broker platforms lack the specific risk-first metrics (like 1R multiples, shadow P&L, and discipline leak costs) essential for systematic day trading. 

By building this tool, I achieved:
1. **Automated Post-Market Review:** Eliminated manual data entry and Excel formatting, instantly generating a comprehensive daily report.
2. **Actionable AI-Coach Diagnostics:** The script autonomously highlights performance gaps (e.g., "Hold Duration Breakdown") to identify emotional biases like holding losers too long.
3. **Strict Accountability:** The dashboard acts as a completely objective accountability partner for risk management challenges.

This project showcases my ability to handle messy raw data, write robust data-cleaning pipelines, and deploy user-centric, data-driven visualization tools.

---
*Created by [CT-TradingJournal] - Continuously building automated tools for disciplined execution.*
