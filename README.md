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

- **[AGENTS.md](./AGENTS.md)** — engineering guide: commands, architecture map, conventions, gotchas
- **[.opencode/context/system-overview.md](./.opencode/context/system-overview.md)** — architecture & API surface
- **[.opencode/context/domain-model.md](./.opencode/context/domain-model.md)** — entities, business rules, glossary
- **[azure_function_catalog/README.md](./azure_function_catalog/README.md)** — scheduled catalog extract deployment
- **[mcp_server/](./mcp_server/)** — MCP server exposing the catalog to AI agents (connection + tool list in [AGENTS.md](./AGENTS.md))

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
   cd PowerBI_Control_Center
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

See [.opencode/context/system-overview.md](./.opencode/context/system-overview.md) for detailed architecture documentation.

---

## 🧪 Testing

### Run the offline unit tests (pytest-style; each file also runs standalone):
```bash
pip install -r requirements-dev.txt
pytest tests/test_expression_parser.py tests/test_pbir_field_walk.py tests/test_crash_schema_validation.py test_crash_test_render_scan.py
```

### Run a single test file without pytest:
```bash
python tests/test_expression_parser.py
```

---

## 📝 Contributing

1. Engineering guidance lives in [AGENTS.md](./AGENTS.md); agent context in `.opencode/`
2. Keep the offline unit tests passing (see Testing above)
3. Include change summaries for all major updates

---

## 📞 Support

For issues, questions, or feature requests, contact the Power BI Control Center Team.

---

## 📄 License

[Add License Information]

---

**Last Updated:** November 20, 2026
**Version:** 2.0.0
**Status:** ✅ Production Ready