from __future__ import annotations

"""
Debug v3 — targeted scan for LinkedIn's SDUI post containers.
"""

import asyncio
import sys
import urllib.parse

from playwright.async_api import async_playwright

from auth import get_authenticated_context, random_delay
from config import Config


async def debug_search(keyword: str):
    Config.validate()

    async with async_playwright() as pw:
        context, auth_ok = await get_authenticated_context(pw)
        if not auth_ok:
            print("[DEBUG] Auth failed.")
            return

        page = await context.new_page()

        encoded = urllib.parse.quote(keyword)
        search_url = (
            f"https://www.linkedin.com/search/results/content/"
            f"?keywords={encoded}&origin=GLOBAL_SEARCH_HEADER&sortBy=date_posted"
        )

        print(f"[DEBUG] Navigating to: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

        print("[DEBUG] Waiting 10 seconds...")
        await asyncio.sleep(10)
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(3)

        # 1. Find repeating patterns (SVG-safe)
        print("\n[DEBUG] === Repeating sibling patterns ===\n")
        repeating = await page.evaluate("""
            () => {
                const candidates = [];
                document.querySelectorAll('ul, ol, div').forEach(parent => {
                    const ch = [...parent.children].filter(c => c.nodeType === 1);
                    if (ch.length >= 2 && ch.length <= 50) {
                        const t = ch[0].tagName;
                        const c = (typeof ch[0].className === 'string' ? ch[0].className : '').split(' ')[0];
                        if (c.length < 2) return;
                        const m = ch.filter(x => {
                            const cls = typeof x.className === 'string' ? x.className : '';
                            return x.tagName === t && cls.includes(c);
                        });
                        if (m.length >= 2) {
                            const pCls = typeof parent.className === 'string' ? parent.className : '';
                            const cCls = typeof ch[0].className === 'string' ? ch[0].className : '';
                            candidates.push({
                                pTag: parent.tagName,
                                pClass: pCls.substring(0, 120),
                                n: m.length,
                                cTag: t,
                                cClass: cCls.substring(0, 120),
                                sample: ch[0].outerHTML.substring(0, 600),
                                textPreview: ch[0].innerText.substring(0, 200)
                            });
                        }
                    }
                });
                return candidates.sort((a, b) => b.n - a.n).slice(0, 15);
            }
        """)
        for r in repeating:
            print(f"  Parent: <{r['pTag']}> class=\"{r['pClass'][:80]}\"")
            print(f"  -> {r['n']}x <{r['cTag']}> class=\"{r['cClass'][:80]}\"")
            print(f"     text: {r['textPreview'][:150]}")
            print(f"     html: {r['sample'][:300]}")
            print()

        # 2. Walk up from known author names
        print("\n[DEBUG] === Ancestor chain from 'Neha chauhan' ===\n")
        chains = await page.evaluate("""
            () => {
                const results = [];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                while (walker.nextNode()) {
                    const text = walker.currentNode.textContent.trim();
                    if (text === 'Neha chauhan' || text === 'Betty Yang') {
                        const el = walker.currentNode.parentElement;
                        let ancestor = el;
                        const chain = [];
                        for (let i = 0; i < 12 && ancestor && ancestor !== document.body; i++) {
                            const cls = typeof ancestor.className === 'string' ? ancestor.className : '';
                            chain.push({
                                tag: ancestor.tagName,
                                cls: cls.substring(0, 100),
                                id: ancestor.id || '',
                                role: ancestor.getAttribute('role') || '',
                                dataAttrs: [...ancestor.attributes]
                                    .filter(a => a.name.startsWith('data-'))
                                    .map(a => a.name + '=' + a.value.substring(0, 60))
                                    .join(', ')
                            });
                            ancestor = ancestor.parentElement;
                        }
                        results.push({ text, chain });
                    }
                }
                return results.slice(0, 4);
            }
        """)
        for nm in chains:
            print(f"  Text: \"{nm['text']}\"")
            for i, a in enumerate(nm['chain']):
                prefix = "  " * i + "└─"
                extras = []
                if a['id']: extras.append(f"id={a['id']}")
                if a['role']: extras.append(f"role={a['role']}")
                if a['dataAttrs']: extras.append(a['dataAttrs'])
                extra_str = f" [{', '.join(extras)}]" if extras else ""
                print(f"    {prefix} <{a['tag']}> .{a['cls'][:70]}{extra_str}")
            print()

        # 3. Find elements with data- attributes (LinkedIn's SDUI markers)
        print("\n[DEBUG] === Elements with data- attributes (SDUI markers) ===\n")
        data_attrs = await page.evaluate("""
            () => {
                const found = [];
                document.querySelectorAll('[data-chameleon-result-urn], [data-view-name], [data-id], [data-finite-scroll-hotkey-item]').forEach(el => {
                    const cls = typeof el.className === 'string' ? el.className : '';
                    found.push({
                        tag: el.tagName,
                        cls: cls.substring(0, 100),
                        attrs: [...el.attributes]
                            .filter(a => a.name.startsWith('data-'))
                            .map(a => a.name + '="' + a.value.substring(0, 80) + '"')
                            .join(' '),
                        text: el.innerText.substring(0, 100)
                    });
                });
                return found.slice(0, 20);
            }
        """)
        for d in data_attrs:
            print(f"  <{d['tag']}> class=\"{d['cls'][:60]}\"")
            print(f"    attrs: {d['attrs'][:200]}")
            print(f"    text: {d['text'][:100]}")
            print()

        if not data_attrs:
            # Broader search
            print("  (none found, trying broader data-* scan...)\n")
            broad = await page.evaluate("""
                () => {
                    const main = document.querySelector('[role="main"]') || document.body;
                    const found = [];
                    main.querySelectorAll('*').forEach(el => {
                        const attrs = [...el.attributes].filter(a => a.name.startsWith('data-'));
                        if (attrs.length > 0 && el.innerText.length > 50 && el.innerText.length < 2000) {
                            const cls = typeof el.className === 'string' ? el.className : '';
                            found.push({
                                tag: el.tagName,
                                cls: cls.substring(0, 80),
                                attrs: attrs.map(a => a.name + '="' + a.value.substring(0, 60) + '"').join(' '),
                                textLen: el.innerText.length,
                                text: el.innerText.substring(0, 120)
                            });
                        }
                    });
                    return found.slice(0, 20);
                }
            """)
            for b in broad:
                print(f"  <{b['tag']}> class=\"{b['cls']}\" textLen={b['textLen']}")
                print(f"    attrs: {b['attrs'][:200]}")
                print(f"    text: {b['text'][:100]}")
                print()

        await page.close()
        await context.close()


if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "battery energy storage"
    asyncio.run(debug_search(keyword))