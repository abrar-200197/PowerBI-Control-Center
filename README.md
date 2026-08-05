# Power BI Control Center

**Automated Documentation & Report Health Diagnostics for Power BI**

A comprehensive web application for generating documentation, performing health diagnostics, and monitoring Power BI reports using the Scanner API, REST API, and JavaScript SDK.

---

## 🎯 Features

### 📄 Automated Documentation Generation
- **Comprehensive Report Documentation:** Generates detailed Word documents for Power BI reports
- **Dataset Schema Analysis:** Documents tables, columns, measures, and relationships
- **DAX Expression Export:** Extracts and documents all DAX measures and calculated columns
- **Visual Metadata:** Captures visual types, configurations, and layouts
- **SQL Query Generation:** Creates sample SQL queries for data sources

### 🔬 Crash Test (Report Health Diagnostics)
- **Granular Visual-Level Error Detection:** Identifies individual broken visuals (not just pages)
- **Dataset Health Check:** Monitors refresh status and failures
- **Schema Integrity Analysis:** Validates tables, columns, and relationships
- **DAX Expression Validation:** Checks measure and calculated column syntax
- **Health Score (0-100):** Quantifiable report health metric
- **Error Reason Mapping:** Human-readable explanations for technical errors

### 🔐 Enterprise Authentication
- **SSO Integration:** Seamless Single Sign-On with Azure AD
- **User-Delegated Permissions:** Access based on user's Power BI permissions
- **Service Principal Support:** Background processing with app credentials

### 🎨 Modern UI
- **Card View:** Visual report cards with metadata
- **Workspace Explorer:** Browse all accessible workspaces
- **Real-time Status:** Live progress indicators for long-running operations
- **Responsive Design:** Works on desktop and tablet devices

---

## 📚 Documentation

All project documentation is organized in the **[`docs/`](./docs/)** directory.

### Quick Links:
- **[Documentation Index](./docs/INDEX.md)** - Master index of all documentation
- **[Crash Test Feature](./docs/CRASH_TEST_FEATURE.md)** - Crash test implementation guide
- **[Latest Update: Granular Breakdown](./docs/CRASH_TEST_GRANULAR_BREAKDOWN_UPDATE.md)** - Visual-level error reporting (June 2026)
- **[Developer Quick Reference](./docs/DEVELOPER_QUICK_REFERENCE.md)** - Common development tasks

---

## 🚀 Getting Started

### Prerequisites
- Python 3.12+
- Power BI Pro or Premium license
- Azure AD App Registration (for authentication)
- Node.js (for Playwright browser automation)

### Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd PowerBI_AI_Docs
   ```

2. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Install Playwright browsers:**
   ```bash
   playwright install chromium
   ```

4. **Configure environment variables:**
   Create a `.env` file in the root directory:
   ```env
   CLIENT_ID=<your-azure-ad-app-client-id>
   CLIENT_SECRET=<your-azure-ad-app-client-secret>
   TENANT_ID=<your-azure-ad-tenant-id>
   REDIRECT_URI=http://localhost:5000/getAToken
   ```

5. **Run the application:**
   ```bash
   python app.py
   ```

6. **Access the application:**
   Open your browser to `http://localhost:5000`

---

## 🔧 Technology Stack

- **Backend:** Python 3.12, Flask
- **Authentication:** MSAL (Microsoft Authentication Library)
- **Power BI APIs:**
  - Scanner API (Admin metadata)
  - REST API (Report & dataset management)
  - JavaScript SDK (Visual-level inspection)
- **Browser Automation:** Playwright
- **Document Generation:** python-docx
- **Frontend:** HTML5, CSS3, JavaScript (vanilla)

---

## 📊 Architecture

The application uses a **Combined Approach** to gather comprehensive metadata:

1. **Scanner API:** Dataset schema, tables, columns, relationships, DAX expressions
2. **REST API:** Report metadata, refresh status, configuration
3. **JavaScript SDK + Playwright:** Visual-level metadata and error detection

See [COMBINED_APPROACH_GUIDE.md](./docs/COMBINED_APPROACH_GUIDE.md) for detailed architecture documentation.

---

## 🧪 Testing

### Run Crash Test (Standalone):
```bash
python test_deep_crash_test.py
```

### Run Specific Report Test:
```bash
python run_crash_test.py
```

---

## 📝 Contributing

1. All documentation must be saved in the `docs/` directory
2. Follow naming conventions in [docs/INDEX.md](./docs/INDEX.md)
3. Update the documentation index when adding new docs
4. Include change summaries for all major updates

---

## 📞 Support

For issues, questions, or feature requests, contact the Power BI Control Center Team.

---

## 📄 License

[Add License Information]

---

**Last Updated:** June 8, 2026
**Version:** 2.0.0
**Status:** ✅ Production Ready