"""
Simple startup script with better error handling
"""
import sys
import traceback

print("\n" + "="*60)
print("🚀 Starting Power BI Documentation Web App")
print("="*60 + "\n")

try:
    print("Step 1: Importing Flask...")
    from flask import Flask
    print("   ✅ Flask imported\n")
    
    print("Step 2: Importing configuration...")
    from config import Config
    print("   ✅ Config imported\n")
    
    print("Step 3: Importing Power BI connector...")
    from powerbi_connector import PowerBIConnector
    print("   ✅ PowerBI connector imported\n")
    
    print("Step 4: Importing document creator...")
    from document_creator import PowerBIDocumentCreator
    print("   ✅ Document creator imported\n")
    
    print("Step 5: Importing AI generator...")
    from ai_generator import AIDocGenerator
    print("   ✅ AI generator imported\n")
    
    print("Step 6: Starting Flask app...")
    from app import app
    print("   ✅ App imported\n")
    
    print("="*60)
    print("✅ All imports successful!")
    print("🌐 Starting web server on http://localhost:5000")
    print("="*60 + "\n")
    
    # Start the app
    app.run(host='0.0.0.0', port=5000, debug=True)
    
except KeyboardInterrupt:
    print("\n\n⚠️  Server stopped by user (Ctrl+C)")
    sys.exit(0)
    
except Exception as e:
    print("\n" + "="*60)
    print("❌ ERROR STARTING APP")
    print("="*60)
    print(f"\nError: {e}\n")
    print("Full traceback:")
    print("-"*60)
    traceback.print_exc()
    print("-"*60)
    print("\n💡 Please share this error message for help!\n")
    sys.exit(1)

