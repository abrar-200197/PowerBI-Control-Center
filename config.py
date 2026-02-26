# config.py
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """Configuration class - stores all settings"""
    
    # Azure App Registration
    TENANT_ID = os.getenv('TENANT_ID')
    CLIENT_ID = os.getenv('CLIENT_ID')
    CLIENT_SECRET = os.getenv('CLIENT_SECRET')
    
    # Power BI User (for delegated permissions)
    POWERBI_USERNAME = os.getenv('POWERBI_USERNAME')
    POWERBI_PASSWORD = os.getenv('POWERBI_PASSWORD')
    
    # OpenAI
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    
    # Power BI
    WORKSPACE_ID = os.getenv('WORKSPACE_ID')
    REPORT_ID=os.getenv('REPORT_ID')
    
    # Output Settings
    OUTPUT_FOLDER = os.getenv('OUTPUT_FOLDER', 'generated_docs')
    AUTHOR_NAME = os.getenv('AUTHOR_NAME', 'System Generated')
    COMPANY_NAME = os.getenv('COMPANY_NAME', 'Ashley Furniture India')
    
    @classmethod
    def validate(cls):
        """Check if all required settings are configured"""
        required_fields = {
            'TENANT_ID': cls.TENANT_ID,
            'CLIENT_ID': cls.CLIENT_ID,
            'OPENAI_API_KEY': cls.OPENAI_API_KEY,
            'WORKSPACE_ID': cls.WORKSPACE_ID
        }
        
        missing = [field for field, value in required_fields.items() if not value]
        
        if missing:
            print("❌ Configuration Error!")
            print(f"Missing values in .env file: {', '.join(missing)}")
            print("\nPlease check your .env file and ensure all values are set correctly.")
            return False
        
        print("✅ Configuration validated successfully!")
        print(f"   Workspace ID: {cls.WORKSPACE_ID[:8]}...")
        print(f"   OpenAI Key: {cls.OPENAI_API_KEY[:10]}...")
        print(f"   Power BI User: {cls.POWERBI_USERNAME}")
        return True
    
    @classmethod
    def display_info(cls):
        """Display current configuration (safely)"""
        print("\n" + "="*50)
        print("CURRENT CONFIGURATION")
        print("="*50)
        print(f"Workspace ID: {cls.WORKSPACE_ID}")
        print(f"Output Folder: {cls.OUTPUT_FOLDER}")
        print(f"Author: {cls.AUTHOR_NAME}")
        print(f"Company: {cls.COMPANY_NAME}")
        print(f"Power BI User: {cls.POWERBI_USERNAME}")
        print("="*50 + "\n")

# Test configuration when this file is run directly
if __name__ == "__main__":
    print("="*60)
    print("CONFIGURATION TEST")
    print("="*60)
    
    if Config.validate():
        Config.display_info()
        print("✅ Configuration test successful!")
    else:
        print("\n❌ Configuration test failed!")
        print("Please check your .env file.")
