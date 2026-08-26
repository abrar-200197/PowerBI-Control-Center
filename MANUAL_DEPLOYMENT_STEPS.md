# 🚀 Manual Deployment Steps - Copy and Paste Commands

Since the automated script has PATH issues, follow these manual steps instead.
Just copy and paste each command into PowerShell.

---

## ✅ Prerequisites Check

Run these first to verify tools are installed:

```powershell
# Test Azure CLI
& 'C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd' --version

# Test Azure Functions Core Tools
& 'C:\Program Files\Microsoft\Azure Functions Core Tools\func.exe' --version
```

If both work, proceed!

---

## 📝 Step 1: Set Variables

**IMPORTANT:** Change these names to something unique for you!

```powershell
# Set your unique resource names
$storageAccountName = "powerbiaidocs2026"  # CHANGE THIS - lowercase, no special chars
$functionAppName = "ashley-powerbi-docs"    # CHANGE THIS - lowercase, hyphens OK
$resourceGroupName = "powerbi-docs-rg"
$location = "eastus"
$containerName = "generated-docs"

# Set tool paths
$az = 'C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd'
$func = 'C:\Program Files\Microsoft\Azure Functions Core Tools\func.exe'
```

---

## 🔐 Step 2: Login to Azure

```powershell
# Login (browser will open)
& $az login
```

**Wait for browser to open, login, then return to PowerShell**

---

## 📦 Step 3: Create Resource Group

```powershell
& $az group create --name $resourceGroupName --location $location
```

✅ **Expected:** You should see `"provisioningState": "Succeeded"`

---

## 💾 Step 4: Create Storage Account

```powershell
& $az storage account create `
    --name $storageAccountName `
    --resource-group $resourceGroupName `
    --location $location `
    --sku Standard_LRS `
    --kind StorageV2
```

⏱️ **This takes 1-2 minutes**

✅ **Expected:** `"provisioningState": "Succeeded"`

---

## 📁 Step 5: Create Blob Container

```powershell
# Get storage key
$storageKey = (& $az storage account keys list `
    --account-name $storageAccountName `
    --resource-group $resourceGroupName `
    --query '[0].value' -o tsv)

# Create container
& $az storage container create `
    --name $containerName `
    --account-name $storageAccountName `
    --account-key $storageKey
```

✅ **Expected:** `"created": true`

---

## ⚡ Step 6: Create Function App

```powershell
& $az functionapp create `
    --resource-group $resourceGroupName `
    --consumption-plan-location $location `
    --runtime python `
    --runtime-version 3.9 `
    --functions-version 4 `
    --name $functionAppName `
    --storage-account $storageAccountName `
    --os-type Linux
```

⏱️ **This takes 2-3 minutes**

✅ **Expected:** `"state": "Running"`

---

## 🔧 Step 7: Configure Environment Variables

**Load credentials from .env file:**

```powershell
# Read .env file
$envFile = Get-Content .env
$envVars = @{}

foreach ($line in $envFile) {
    if ($line -match '^\s*([^#][^=]+)=(.+)$') {
        $key = $matches[1].Trim()
        $value = $matches[2].Trim()
        $envVars[$key] = $value
    }
}

# Extract variables
$clientId = $envVars['CLIENT_ID']
$clientSecret = $envVars['CLIENT_SECRET']
$tenantId = $envVars['TENANT_ID']
$workspaceId = $envVars['WORKSPACE_ID']
$openaiApiKey = $envVars['OPENAI_API_KEY']
$authorName = $envVars['AUTHOR_NAME']

# Set environment variables in Azure
& $az functionapp config appsettings set `
    --name $functionAppName `
    --resource-group $resourceGroupName `
    --settings `
        CLIENT_ID="$clientId" `
        CLIENT_SECRET="$clientSecret" `
        TENANT_ID="$tenantId" `
        WORKSPACE_ID="$workspaceId" `
        OPENAI_API_KEY="$openaiApiKey" `
        AUTHOR_NAME="$authorName" `
        OUTPUT_FOLDER="/tmp/generated_docs" `
        HISTORY_TOP="20" `
        USE_OPENAI="true"
```

✅ **Expected:** JSON output with all your settings

---

## 📤 Step 8: Deploy Your Code

```powershell
& $func azure functionapp publish $functionAppName --python
```

⏱️ **This takes 3-5 minutes**

✅ **Expected:** "Deployment successful"

---

## 🔗 Step 9: Get Webhook URL

```powershell
# Get function keys
$functionKeys = & $az functionapp keys list `
    --name $functionAppName `
    --resource-group $resourceGroupName | ConvertFrom-Json

$functionKey = $functionKeys.functionKeys.default

# Construct webhook URL
$webhookUrl = "https://$functionAppName.azurewebsites.net/api/webhook?code=$functionKey"

# Display URL
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "YOUR WEBHOOK URL:" -ForegroundColor Cyan
Write-Host $webhookUrl -ForegroundColor White
Write-Host "============================================" -ForegroundColor Green
Write-Host ""

# Save to .env file
$envContent = Get-Content .env
$envContent += ""
$envContent += "# Azure Function URL (added during deployment)"
$envContent += "AZURE_FUNCTION_URL=$webhookUrl"
$envContent | Set-Content .env

Write-Host "✓ Webhook URL saved to .env file" -ForegroundColor Green
```

---

## 🎉 Deployment Complete!

Now register the webhooks:

```powershell
python register_webhook.py
```

In the menu:
1. Press **1** → Register for "Report.Published"
2. Press **2** → Register for "Report.Updated"
3. Press **4** → Verify webhooks
4. Press **6** → Exit

---

## 🧪 Test It!

Publish a Power BI report and watch the magic happen!

View logs:
```powershell
& $func azure functionapp logstream $functionAppName
```

---

**That's it! Your automatic documentation system is live!** 🚀

