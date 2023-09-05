import argparse
import datetime
import logging
import re
from functools import partial

from google.cloud import firestore
from praw.exceptions import APIException

from llm import build_llm_plan_response_text
from matching import RuleStrategy, Strategy
from plans import Plan, PlanCluster, PurePlan
from reddit_util import standardize

logger = logging.getLogger(__name__)


def parent_reply_prefix(post):
    return f"/u/{post.author.name} asked me to chime in!" f"\n\n"


def footer():
    return (
        f"\n\n"
        # Horizontal line above footer
        "\n***\n"
        # Disclaimer
        f"This bot was created independently by volunteers. [Join us!](https://elizabethwarren.com/join-us) "
    )


def _plan_links(plans: list[Plan]) -> str:
    return "\n".join(
        ["[" + plan["display_title"] + "](" + plan["url"] + ")  " for plan in plans]
    )


def build_response_text_plan_cluster(plan_cluster: PlanCluster):
    """
    Create response text with plan summary when plan is actually a plan cluster
    """

    return (
        f"Senator Warren has quite a number of plans for that!"
        f"\n\n"
        # Links to learn more about the plan cluster
        f"Learn more about her plans for {plan_cluster['display_title']}:"
        f"\n\n"
        f"{ _plan_links(plan_cluster['plans'])}"
        f"{footer()}"
    )


def build_response_text_pure_plan(plan: PurePlan):
    """
    Create response text with plan summary
    """

    return (
        f"Senator Warren has a plan for that!"
        f"\n\n"
        f"{plan['summary']}"
        f"\n\n"
        # Link to learn more about the plan
        f"Learn more about her plan: [{plan['display_title']}]({plan['url']})"
        f"{footer()}"
    )


def build_plan_response_text(plan: Plan, full_post_text: str) -> (str, str):
    """
    Build response text for plan matches

    :return: (response_text, reply_type)
    """
    # use static response text if match is a cluster
    if plan.get("is_cluster"):
        return build_response_text_plan_cluster(plan), "plan_cluster"

    # if single plan match, try building a response using llm
    #  provide the entire text of the post for context of any specific
    #  questions asked etc...
    llm_response = build_llm_plan_response_text(plan, full_post_text)

    if llm_response:
        return llm_response, "plan_llm"

    # if llm failed for any reason, fallback to static response text
    return build_response_text_pure_plan(plan), "plan"


def build_verbatim_response_text(verbatim):
    return f"""{verbatim["text"]}{footer()}"""


def build_no_match_response_text(potential_plan_matches: list[Plan], post):
    if potential_plan_matches:
        return (
            f"I'm not sure I have an exact match for you! "
            f"Here are the plans that seem most relevant:"
            f"\n\n"
            f"{ _plan_links(match['plan'] for match in potential_plan_matches[:8])}"
            f"\n\n"
            f"Or I can show you my full list of her plans if you reply with"
            f"\n\n"
            f"```"
            f"!WarrenPlanBot show me the plans"
            f"```"
            f"\n\n"
            f"{footer()}"
        )
    else:
        return (
            f"I'm not sure exactly which plan you're looking for, "
            f"and I'm not feeling confident enough in any of my guesses to tell you about them! ':("
            f"\n\n"
            f"I can show you my full list of her plans if you reply with"
            f"\n\n"
            f"```"
            f"!WarrenPlanBot show me the plans"
            f"```"
            f"\n\n"
            f"Or please kindly rephrase? ':D"
            f"{footer()}"
        )


def build_all_plans_response_text(plans: list[Plan]) -> str:
    pure_plans = list(filter(lambda p: not p.get("is_cluster"), plans))

    response = (
        f"Here's the full list of plans Sen. Warren has released that I know about:"
        f"\n\n"
        f"|[{pure_plans[0]['display_title']}]({pure_plans[0]['url']})|[{pure_plans[1]['display_title']}]({pure_plans[1]['url']})|[{pure_plans[2]['display_title']}]({pure_plans[2]['url']})|"
        f"\n"
        f"|:-:|:-:|:-:|"
        f"\n"
    )
    for i, plan in enumerate(pure_plans[3:], start=3):
        response += f"|[{plan['display_title']}]({plan['url']})"
        if (i + 1) % 3 == 0:
            response += "|\n"

    response += f"\n\n" f"{footer()}"

    return response


def day_suffix(d):
    return "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")


def custom_strftime(format, t):
    return t.strftime(format).replace("{S}", str(t.day) + day_suffix(t.day))


def build_state_of_race_response_text(today: datetime.date) -> str:
    if today > datetime.date(2020, 3, 6):
        return "rip."

    current_delegates_awarded = 101
    total_pledged_delegates = 3_979
    delegate_percentage_left = round(
        (1 - current_delegates_awarded / total_pledged_delegates) * 100
    )

    today_text = custom_strftime("%b {S}", today)

    response = (
        f"As of {today_text}, only {current_delegates_awarded} out of {total_pledged_delegates:,} total delegates have been awarded in the primary. "
        f"That means {delegate_percentage_left}% of the delegates are still up for grabs!"
        f"\n\n"
        f"[Be part of Warren’s surge in support!](https://elizabethwarren.com/join-us)"
    )
    return response


def reply(post, reply_string: str, parent=False, send=False, simulate=False):
    """
    :param post: post to reply on
    :param reply_string: string to reply with
    :param send: whether to send an actual reply to reddit
    :param simulate: whether to simulate sending an actual reply to reddit
    :return: did_reply – whether an actual or simulated reply was made
    """

    if parent and hasattr(post, "parent"):
        post = standardize(post.parent())

    logger.debug(reply_string)
    if simulate:
        logger.info(f"[simulated] Bot replying to {post.type}: {post.id}")
        return True
    if send:
        logger.info(f"Bot replying to {post.type}: {post.id}")
        post.reply(reply_string)
        return True

    logger.info(f"Bot would have replied to {post.type}: {post.id}")


def process_post(
    post,
    plans,
    verbatims,
    posts_db,
    post_ids_processed=None,
    send=False,
    simulate=False,
    skip_tracking=False,
    matching_strategy=Strategy.lsa_gensim_v3,
):
    if post_ids_processed is None:
        post_ids_processed = set()

    # Make sure we don't reply to a post we've already processed
    if post.id in post_ids_processed:
        return

    logger.info(f"Processing post {post.type}: {post.id}")

    # Add this post to the set of processed posts
    post_ids_processed.add(post.id)

    # Never try to reply if a post is locked
    if post.locked:
        skip_reason = "post_locked"
    # Never reply to a deleted post
    elif not post.author:
        skip_reason = "no_author"
    # Make sure we're not replying to ourself
    elif "warrenplanbot" in post.author.name.lower():
        skip_reason = "own_post"
    # Ensure it's a post where someone summoned us
    elif not re.search("!warrenplanbot", post.text, re.IGNORECASE):
        skip_reason = "trigger_not_found"
    else:
        skip_reason = None

    if skip_reason:
        post_record = create_db_record(
            post, processed=True, skipped=True, skip_reason=skip_reason
        )
        if not skip_tracking:
            posts_db.document(post.id).set(post_record)
        return

    post_text, options = process_flags(get_trigger_line(post.text))

    match_info = (
        RuleStrategy.match_verbatim(verbatims, post_text, options)
        or RuleStrategy.request_plan_list(plans, post_text, post=post)
        or RuleStrategy.request_state_of_race(plans, post_text, post=post)
        or RuleStrategy.match_display_title(plans, post_text, post=post)
        or matching_strategy(plans, post_text, post=post)
    )

    match = match_info.get("match")
    operation = match_info.get("operation")
    plan_confidence = match_info.get("confidence")
    plan = match_info.get("plan", {})
    potential_matches = match_info.get("potential_matches")
    plan_id = plan.get("id")
    verbatim = match_info.get("verbatim", {})
    verbatim_id = verbatim.get("id")

    # Create partial db entry from known values, placeholder defaults for mutable values
    # Mark post as processed _before_ we reply to prevent double-posting
    post_record = create_db_record(
        post, match, plan_confidence, plan_id, verbatim_id, processed=True
    )

    if not skip_tracking:
        posts_db.document(post.id).set(post_record)

    operations_map = {
        "verbatim": partial(build_verbatim_response_text, verbatim),
        "all_the_plans": partial(build_all_plans_response_text, plans),
        "state_of_race": partial(
            build_state_of_race_response_text, datetime.date.today()
        ),
    }

    post_record_update = {}

    # If plan is matched with confidence, build and send reply
    if match:
        logger.info(f"plan match: {plan_id} {post.id} {plan_confidence}")

        reply_string, reply_type = build_plan_response_text(plan, post.text)
        post_record_update["reply_type"] = reply_type
    elif operation and operation in operations_map:
        logger.info(f"{operation} requested: {post.id}")

        response_fn = operations_map[operation]
        reply_string = response_fn()
        post_record_update["reply_type"] = "operation"
        post_record_update["operation"] = operation
    else:
        logger.info(f"topic mismatch: {plan_id} {post.id} {plan_confidence}")

        reply_string = build_no_match_response_text(potential_matches, post)
        post_record_update["reply_type"] = "no_match"

    # add prefix with info about calling post if this is a parent operation
    if "parent" in options:
        reply_string = parent_reply_prefix(post) + reply_string

    try:
        did_reply = reply(
            post, reply_string, parent="parent" in options, send=send, simulate=simulate
        )
    except APIException as e:
        if e.error_type == "DELETED_COMMENT":
            did_reply = False
            post_record_update["skipped"] = True
            post_record_update["skip_reason"] = "deleted_comment"
        else:
            raise

    post_record_update["replied"] = did_reply
    if did_reply:
        # Replace default None values in post_record_update record
        post_record_update["reply_timestamp"] = firestore.SERVER_TIMESTAMP

    if not skip_tracking:
        posts_db.document(post.id).update(post_record_update)


def create_db_record(
    post,
    match=None,
    plan_confidence=None,
    plan_id=None,
    verbatim_id=None,
    reply_timestamp=None,
    reply_made=False,
    processed=False,
    skipped=False,
    skip_reason=None,
) -> dict:
    # Reddit 3-digit code prefix removed for each id, leaving only the ID itself
    post_parent_id = post.parent_id[3:] if post.type == "comment" else None
    post_subreddit_id = post.subreddit.name[3:]
    post_top_level_parent_id = post.link_id[3:] if post.type == "comment" else None
    post_title = post.title if post.type == "submission" else None
    # Return db_entry for Firestore
    entry = {
        "processed": processed,
        "processed_timestamp": firestore.SERVER_TIMESTAMP,
        "replied": reply_made,
        "skipped": skipped,
        "skip_reason": skip_reason,
        "type": post.type,
        "post_id": post.id,
        "post_author": "/u/" + post.author.name,
        "post_text": post.text,
        "post_parent_id": post_parent_id,  # ID or None if no parent_id
        "post_url": "https://www.reddit.com" + post.permalink,
        "post_subreddit_id": post_subreddit_id,
        "post_subreddit_display_name": post.subreddit.display_name,
        "post_title": post_title,  # Post Title or None if no title
        "post_top_level_parent_id": post_top_level_parent_id,
        "post_locked": post.locked,
        # TODO flesh out / clarify this some
        "plan_match": match,
        "top_plan_confidence": plan_confidence,
        "top_plan": plan_id,
        "verbatim_id": verbatim_id,
        "reply_timestamp": reply_timestamp,
    }

    return entry


def get_trigger_line(text: str, trigger_word="!warrenplanbot") -> str:
    """
    Get the final sentance that !WarrenPlanBot occurs in,
    only returning the part of that sentance which occurs _after_ !WarrenPlanBot
    """
    matches = re.findall(
        rf"{trigger_word}[^-\w]+([^!?.]*[!?.]?)", text, re.IGNORECASE | re.MULTILINE
    )
    if not matches:
        return ""

    return matches[-1]


def process_flags(text):
    """
    Identifies flags in the text. Removes the flags from the text and
    returns the tuple (remaining_text, options).
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent", "--tell-parent", action="store_true")
    parser.add_argument("--why-warren", action="store_true")
    parser.add_argument(
        "--state-of-race", "--state-of-the-race", "--status-check", action="store_true"
    )
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    options, unknown = parser.parse_known_args(text.split())
    remaining_text = " ".join(options.rest)
    del options.rest  # we don't need this, and removing it makes testing easier
    return (
        remaining_text,
        {flag for flag, is_true in vars(options).items() if is_true},
    )
