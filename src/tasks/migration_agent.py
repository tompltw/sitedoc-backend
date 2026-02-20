"""
Migration Agent — Crawls an existing website and produces a structured migration spec.

The migration spec is used by the dev agent (via a site_build issue) to rebuild the site
on SiteDoc managed hosting with a modern theme and improved design.

Trigger: Customer submits a migration request with their existing site URL.
"""
import json
import logging
import os
import re
import uuid
from typing import Optional
from urllib.parse import urljoin, urlparse

from src.db.models import AgentAction, ActionStatus
from src.tasks.base import celery_app, get_db_session, post_chat_message
from src.tasks.llm import call_llm

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")


def _crawl_site(url: str, max_pages: int = 20) -> dict:
    """
    Crawl a website and extract structure, content, and design information.
    Returns a structured migration spec.
    """
    import httpx
    from bs4 import BeautifulSoup

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    visited = set()
    pages = []
    nav_links = []
    colors = set()
    fonts = set()
    images = []

    def _extract_page(page_url: str, depth: int = 0) -> Optional[dict]:
        if page_url in visited or len(visited) >= max_pages:
            return None
        if urlparse(page_url).netloc != parsed.netloc:
            return None

        visited.add(page_url)
        try:
            resp = httpx.get(page_url, timeout=15, follow_redirects=True)
            if resp.status_code != 200:
                return None
            if 'text/html' not in resp.headers.get('content-type', ''):
                return None
        except Exception as e:
            logger.warning("[migration] Failed to fetch %s: %s", page_url, e)
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Extract title
        title = soup.title.string.strip() if soup.title and soup.title.string else ''

        # Extract meta description
        meta_desc = ''
        meta_tag = soup.find('meta', attrs={'name': 'description'})
        if meta_tag and meta_tag.get('content'):
            meta_desc = meta_tag['content']

        # Extract main content text
        # Remove script, style, nav, header, footer elements
        for tag in soup.find_all(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
            tag.decompose()

        main_content = soup.find('main') or soup.find('article') or soup.find(id='content') or soup.body
        text_content = main_content.get_text(separator='\n', strip=True)[:3000] if main_content else ''

        # Extract headings
        headings = []
        for h in soup.find_all(['h1', 'h2', 'h3']):
            headings.append({'level': h.name, 'text': h.get_text(strip=True)[:200]})

        # Extract images (first 10 per page)
        page_images = []
        for img in soup.find_all('img')[:10]:
            src = img.get('src', '')
            if src:
                full_src = urljoin(page_url, src)
                alt = img.get('alt', '')
                page_images.append({'src': full_src, 'alt': alt})
                images.append({'src': full_src, 'alt': alt, 'page': page_url})

        # Extract internal links for further crawling
        internal_links = []
        for a in soup.find_all('a', href=True):
            href = urljoin(page_url, a['href'])
            if urlparse(href).netloc == parsed.netloc:
                clean_href = href.split('#')[0].split('?')[0].rstrip('/')
                if clean_href not in visited and clean_href != page_url.rstrip('/'):
                    internal_links.append(clean_href)

        # Extract colors from inline styles
        for style_tag in soup.find_all('style'):
            if style_tag.string:
                color_matches = re.findall(r'#[0-9a-fA-F]{3,6}', style_tag.string)
                colors.update(color_matches[:20])

        # Extract fonts from stylesheets
        for link in soup.find_all('link', rel='stylesheet'):
            href = link.get('href', '')
            if 'fonts.googleapis.com' in href:
                font_match = re.findall(r'family=([^&:]+)', href)
                for f in font_match:
                    fonts.add(f.replace('+', ' '))

        page_data = {
            'url': page_url,
            'title': title,
            'meta_description': meta_desc,
            'headings': headings[:10],
            'content_preview': text_content[:1500],
            'images': page_images,
            'internal_links': internal_links[:20],
        }

        # Crawl linked pages (depth-limited)
        if depth < 2:
            for link in internal_links[:10]:
                _extract_page(link, depth + 1)

        return page_data

    # Extract navigation from homepage
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        soup = BeautifulSoup(resp.text, 'html.parser')
        nav = soup.find('nav') or soup.find(role='navigation')
        if nav:
            for a in nav.find_all('a', href=True):
                nav_links.append({
                    'text': a.get_text(strip=True),
                    'url': urljoin(url, a['href']),
                })
    except Exception:
        pass

    # Crawl starting from homepage
    home_page = _extract_page(url)
    if home_page:
        pages.append(home_page)

    # Crawl pages found in navigation
    for link in nav_links:
        page = _extract_page(link['url'])
        if page:
            pages.append(page)

    return {
        'source_url': url,
        'pages': pages,
        'navigation': nav_links,
        'colors': list(colors)[:20],
        'fonts': list(fonts)[:10],
        'images': images[:50],
        'total_pages_found': len(visited),
    }


def _generate_migration_spec(crawl_data: dict) -> str:
    """
    Use LLM to analyze crawl data and produce a structured migration spec
    that can be used as a site_build issue description.
    """
    crawl_summary = json.dumps(crawl_data, indent=2)[:8000]

    system_prompt = """You are a web migration specialist. Analyze the crawled website data and produce a structured migration spec for rebuilding the site on WordPress.

Your output should be a clear, actionable build specification that a developer can follow to recreate this site. Include:

1. **Site Overview**: What kind of site is this? What's the business?
2. **Pages to Create**: List each page with its purpose and key content sections
3. **Navigation Structure**: Main menu items and their order
4. **Design Notes**: Colors, fonts, overall style/mood
5. **Content Summary**: Key text content for each page (summarized, not copied verbatim)
6. **Features Needed**: Contact forms, galleries, maps, etc.
7. **Recommended Theme**: Which WordPress theme would best match this design
8. **Recommended Plugins**: What plugins are needed

Be specific and actionable. This spec will be given directly to a developer."""

    messages = [
        {"role": "user", "content": f"Analyze this crawled website data and create a migration spec:\n\n{crawl_summary}"}
    ]

    resp = call_llm(system_prompt=system_prompt, messages=messages)
    return resp.content.strip()


@celery_app.task(name="src.tasks.migration_agent.crawl_and_spec")
def crawl_and_spec(issue_id: str, source_url: str) -> None:
    """
    Crawl an existing site and generate a migration spec.
    Updates the issue description with the spec for the dev agent.
    """
    logger.info("[migration] Starting crawl of %s for issue %s", source_url, issue_id)

    try:
        post_chat_message(
            issue_id,
            f"Crawling {source_url} to analyze the site structure and content...",
            "system",
            DB_URL,
        )

        # 1. Crawl the site
        crawl_data = _crawl_site(source_url)
        logger.info("[migration] Crawled %d pages from %s", len(crawl_data['pages']), source_url)

        post_chat_message(
            issue_id,
            f"Found {len(crawl_data['pages'])} pages. Analyzing content and generating build spec...",
            "system",
            DB_URL,
        )

        # 2. Generate migration spec via LLM
        migration_spec = _generate_migration_spec(crawl_data)

        # 3. Update issue description with the spec
        from src.db.models import Issue

        with get_db_session(DB_URL) as session:
            issue = session.get(Issue, uuid.UUID(issue_id))
            if issue:
                existing = issue.description or ""
                issue.description = (
                    f"{existing}\n\n"
                    f"---\n"
                    f"## Migration Spec (auto-generated from {source_url})\n\n"
                    f"{migration_spec}"
                )

            # Log the action
            session.add(AgentAction(
                issue_id=uuid.UUID(issue_id),
                action_type="site_crawl",
                description=f"Crawled {source_url}: {len(crawl_data['pages'])} pages analyzed",
                status=ActionStatus.completed,
                after_state=json.dumps({
                    'pages_crawled': len(crawl_data['pages']),
                    'colors_found': len(crawl_data['colors']),
                    'fonts_found': len(crawl_data['fonts']),
                    'images_found': len(crawl_data['images']),
                })[:2000],
            ))

        post_chat_message(
            issue_id,
            f"Migration analysis complete! I've analyzed {len(crawl_data['pages'])} pages from your existing site. "
            "The build spec has been generated and the dev team will use it to recreate your site with a modern design.",
            "system",
            DB_URL,
        )

        logger.info("[migration] Migration spec generated for issue %s", issue_id)

    except Exception as e:
        logger.exception("[migration] Failed to crawl %s for issue %s: %s", source_url, issue_id, e)
        try:
            post_chat_message(
                issue_id,
                f"I encountered an error while analyzing your existing site. "
                "Our team has been notified and will help manually.",
                "system",
                DB_URL,
            )
            with get_db_session(DB_URL) as session:
                session.add(AgentAction(
                    issue_id=uuid.UUID(issue_id),
                    action_type="site_crawl",
                    description=f"Failed to crawl {source_url}: {str(e)[:300]}",
                    status=ActionStatus.failed,
                ))
        except Exception:
            pass
