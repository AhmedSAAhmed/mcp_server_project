
from IPython.utils import encoding
from unittest import result
from traitlets.utils import descriptions
from numpy._core.defchararray import title
import os
import json
import logging
from typing import List, Dict, Optional
from firecrawl import FirecrawlApp
from urllib.parse import urlparse
from datetime import datetime
from mcp.server.fastmcp import FastMCP

from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

SCRAPE_DIR = "scraped_content"

mcp = FastMCP("llm_inference")

@mcp.tool()
def scrape_websites(
    websites: Dict[str, str],
    formats: List[str] = ['markdown', 'html'],
    api_key: Optional[str] = None
) -> List[str]:
    """
    Scrape multiple websites using Firecrawl and store their content.
    
    Args:
        websites: Dictionary of provider_name -> URL mappings
        formats: List of formats to scrape ['markdown', 'html'] (default: both)
        api_key: Firecrawl API key (if None, expects environment variable)
        
    Returns:
        List of provider names for successfully scraped websites
    """
    
    if api_key is None:
        api_key = os.getenv('FIRECRAWL_API_KEY')
        if not api_key:
            raise ValueError("API key must be provided or set as FIRECRAWL_API_KEY environment variable")
    
    app = FirecrawlApp(api_key=api_key)
    
    path = os.path.join(SCRAPE_DIR)
    os.makedirs(path, exist_ok=True)
    
    # save the scraped content to files and then create scraped_metadata.json as a summary file
    # check if the provider has already been scraped and decide if you want to overwrite
    # {
    #     "cloudrift_ai": {
    #         "provider_name": "cloudrift_ai",
    #         "url": "https://www.cloudrift.ai/inference",
    #         "domain": "www.cloudrift.ai",
    #         "scraped_at": "2025-10-23T00:44:59.902569",
    #         "formats": [
    #             "markdown",
    #             "html"
    #         ],
    #         "success": "true",
    #         "content_files": {
    #             "markdown": "cloudrift_ai_markdown.txt",
    #             "html": "cloudrift_ai_html.txt"
    #         },
    #         "title": "AI Inference",
    #         "description": "Scraped content goes here"
    #     }
    # }
    metadata_file = os.path.join(path, "scraped_metadata.json")

    try:
        with open(metadata_file, "r", encoding="utf-8") as f:
            scraped_metadata = json.load(f)
            if not isinstance(scraped_metadata, dict):
                scraped_metadata = {}
    except (FileNotFoundError, json.JSONDecodeError):
        scraped_metadata = {}

    successful_scrapes = []

    for provider_name, url in websites.items():
        metadata = {
            "provider_name": provider_name,
            "url": url,
            "domain": urlparse(url).netloc,
            "scraped_at": datetime.now().isoformat(),
            "formats": formats,
            "success": False,
        }
        try:
            logger.info(f"Scraping {provider_name}: {url}")
            scrape_result = app.scrape(url, formats=formats).model_dump()
            if scrape_result.get("success", False) or scrape_result.get("markdown") or scrape_result.get("html"):
                metadata["success"] = True
                content_files = {}

                for format_type in formats:
                    content = scrape_result.get(format_type)
                    if content:
                        filename = f"{provider_name}_{format_type}.txt"
                        file_path = os.path.join(path, filename)
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        content_files[format_type] = filename

                metadata["content_files"] = content_files
                result_meta = scrape_result.get("metadata") or {}
                if not isinstance(result_meta, dict):
                    result_meta = {}
                metadata["title"] = scrape_result.get("title") or result_meta.get("title") or ""
                metadata["description"] = scrape_result.get("description") or result_meta.get("description") or ""
                successful_scrapes.append(provider_name)
            else:
                error_msg = scrape_result.get("error", "Unknown error")
                logger.error(f"Failed to scrape {provider_name}: {error_msg}")
                metadata["error"] = error_msg
        except Exception as e:
            logger.error(f"Exception while scraping {provider_name} ({url}): {e}")
            metadata["error"] = str(e)
        finally:
            scraped_metadata[provider_name] = metadata

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(scraped_metadata, f, indent=4)
    logger.info(f"Scraping completed. Successfully scraped {len(successful_scrapes)}/{len(websites)} websites.")
    return successful_scrapes
            


    

@mcp.tool()
def extract_scraped_info(identifier: str) -> str:
    """
    Extract information about a scraped website.
    
    Args:
        identifier: The provider name, full URL, or domain to look for
        
    Returns:
        Formatted JSON string with the scraped information
    """
    
    logger.info(f"Extracting information for identifier: {identifier}")

    metadata_file = os.path.join(SCRAPE_DIR, "scraped_metadata.json")
    logger.info(f"Checking metadata file: {metadata_file}")

    try:
        with open(metadata_file, "r", encoding="utf-8") as f:
            scraped_metadata = json.load(f)

        for provider_name, metadata in scraped_metadata.items():
            if identifier in (
                provider_name,
                metadata.get("url", ""),
                metadata.get("domain", ""),
            ):
                result = metadata.copy()
                if "content_files" in metadata:
                    result["content"] = {}
                    for format_type, filename in metadata["content_files"].items():
                        file_path = os.path.join(SCRAPE_DIR, filename)
                        
                        with open(file_path, "r", encoding="utf-8") as content_file:
                            result["content"][format_type] = content_file.read()
                return json.dumps(result, indent=2)

    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        logger.error(f"Error reading scraped metadata or content for {identifier} : {e}")

    return f"There's no saved information related to identifier {identifier}" 

if __name__ == "__main__":
    mcp.run(transport="stdio")