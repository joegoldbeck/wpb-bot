#!/usr/bin/env python3

import glob
import json
import logging
import os
import re
from os import path
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment
from unidecode import unidecode

DIRNAME = path.dirname(path.realpath(__file__))

OUTPUT_DIR = path.abspath(path.join(DIRNAME, "../src/plan_text"))

PLAN_HTML_DIR = path.abspath(path.join(DIRNAME, "../data/raw/plan_html"))

plan_file_paths = [f for f in glob.glob(path.join(PLAN_HTML_DIR, "*"))]

logging.basicConfig(format="%(levelname)s : %(message)s", level=logging.INFO)

logger = logging.getLogger(__name__)


def clear_output_dir():
    """
    Make empty output directory
    """
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    for file_path in os.listdir(OUTPUT_DIR):
        os.remove(path.join(OUTPUT_DIR, file_path))


def unwrap_and_smooth(soup, tag):
    """
    Remove html tag, preserving contents as text
    """
    # remove tag
    for t in soup.findAll(tag):
        t.unwrap()
    # smooth together the text that was in those tags with neighboring text
    soup.smooth()


def decompose_and_smooth(soup, tag, **kwargs):
    """
    Remove html tag including any contents
    """
    # remove tag
    for t in soup.findAll(tag, **kwargs):
        t.decompose()
    # smooth together the text that was in those tags with neighboring text
    soup.smooth()


def decompose_and_smooth_first(soup, tag, **kwargs):
    """
    Remove first matching html tag including any contents
    """
    # remove tag
    t = soup.find(tag, **kwargs)
    if t:
        t.decompose()
    # smooth together the text that was in those tags with neighboring text
    soup.smooth()


def remove_html_comments(soup):
    """
    Remove html comments entirely ("<---blah--->")
    """
    comments = soup.findAll(text=lambda text: isinstance(text, Comment))
    for comment in comments:
        comment.extract()

    soup.smooth()


def parse_articles(soup):
    """
    Parse any plans that have a nice article tags

    Warren.com, Medium posts and WashingtonPost fall under this category neatly
    """

    # remove table of contents
    decompose_and_smooth_first(
        soup,
        "article",
        class_=re.compile(r".*DetailPageTableOfContentsBlocks__Container.*"),
    )

    # remove calculator section
    decompose_and_smooth_first(
        soup, "p", text=re.compile(r"Use this handy calculator.*")
    )
    decompose_and_smooth_first(soup, "a", text=re.compile(r"calculator"))

    return "\n".join(parse_article(article) for article in soup.findAll("article"))


def parse_article(article):
    """
    Parse any plans that have a nice article tag

    Medium posts and WashingtonPost fall under this category neatly

    Essence has some very minor issues here because they have the first word of a paragraph
    often separated from the rest of the paragraph
    """
    for tag in ["a", "b", "i", "u", "em", "strong"]:
        unwrap_and_smooth(article, tag)

    for tag in ["noscript", "img", "button"]:
        decompose_and_smooth(article, tag)

    # remove sign up sections
    decompose_and_smooth(
        article, "div", class_=re.compile(r".*PlanSignupInterruptorBlocks.*")
    )

    # remove "As published on Medium..."
    decompose_and_smooth_first(
        article, "p", text=re.compile(r"As published (on Medium|in Essence).*")
    )

    # remove paragraphs like "Read expert letter on cost estimate of Medicare for All here"
    decompose_and_smooth(article, "p", text=re.compile(r"Read expert letter.*here"))

    remove_html_comments(article)

    return article.get_text(separator="\n")


def _flatten(x):
    if isinstance(x, list):
        return [a for i in x for a in _flatten(i)]
    else:
        return [x]


def _get_contents(contents):
    if isinstance(contents, list):
        return [_get_contents(content) for content in contents]
    if isinstance(contents, dict):
        if "value" in contents:
            return contents["value"]
        elif "content" in contents:
            return _get_contents(contents["content"])
        else:
            raise NotImplementedError(f"dont know how to parse {contents}")


def parse_medium_dot_com(soup):
    print("Parsing Medium article")
    text = parse_articles(soup)
    # manually clean up the one medium article that we're currently parsing
    text_to_strip = "No President Is Above the Law\nTeam Warren\nMay 31 · 5 min read\nBy Elizabeth Warren\n"
    if text.startswith(text_to_strip):
        return text[len(text_to_strip) :]
    return text


def parse_plans():
    """
    Extract text from plan html, preserving whitespace as appropriate

    Optimized for Medium posts
    """

    clear_output_dir()

    # Iterate through plans
    for plan_file_path in plan_file_paths:
        logger.info(f"Parsing {plan_file_path}")

        with open(plan_file_path) as plan_file:
            plan = json.load(plan_file)
        plan_id = plan["id"]

        filename = path.join(OUTPUT_DIR, f"{plan_id}.json")

        # if we already have the "full text" of a plan in plans.json for some reason or another
        if plan.get("full_text"):
            logger.info(f"Full text already available. Writing to {filename}")
            with open(filename, "w") as plan_text_file:
                json.dump(
                    {
                        "text": plan.get("full_text"),
                        "url": plan["url"],
                        "id": plan["id"],
                    },
                    plan_text_file,
                    sort_keys=True,
                    indent=4,
                    separators=(",", ": "),
                )
            continue

        html = plan["html"]

        plan_hostname = urlparse(plan["url"]).netloc

        page_soup = BeautifulSoup(html, "lxml")

        if "medium" in plan_hostname:
            text = parse_medium_dot_com(page_soup)
        elif page_soup.find("article"):
            text = parse_articles(page_soup)
        else:
            logger.warning(
                f"Failure to parse {plan_id}. Hostname: {plan_hostname} is not yet supported"
            )
            continue

        # replace smart quotes and em-dashes ... with their ascii equivalents
        text = unidecode(text)

        logger.info(f"Writing plan text to {filename}")
        with open(filename, "w") as plan_text_file:
            json.dump(
                {"text": text, "url": plan["url"], "id": plan["id"]},
                plan_text_file,
                sort_keys=True,
                indent=4,
                separators=(",", ": "),
            )


if __name__ == "__main__":
    parse_plans()
