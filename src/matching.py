import json
import logging
import re
from functools import lru_cache, partial
from os import path

from fuzzywuzzy import fuzz
from gensim import corpora, models, similarities
from gensim.parsing.preprocessing import STOPWORDS as GENSIM_STOPWORDS
from gensim.parsing.preprocessing import (
    preprocess_string,
    stem_text,
    strip_multiple_whitespaces,
    strip_numeric,
    strip_punctuation,
    strip_short,
)
from unidecode import unidecode

DIRNAME = path.dirname(path.realpath(__file__))

GENSIM_V3_MODELS_PATH = path.abspath(path.join(DIRNAME, "models/gensim_strategy_v3"))

# suppress gensim logs
logging.getLogger("gensim").setLevel(logging.WARNING)


MODIFIED_GENSIM_STOPWORDS = set().union(GENSIM_STOPWORDS) - {"all"}

CUSTOM_STOPWORDS = {
    "elizabeth",
    "warren",
    "plan",
    "warrenplanbot",
    "warrenplanbotdev",
    "sen",
    "senator",
    "thanks",
    "thank",
    "you",
    "show",
}


class Preprocess:
    """
    Defines strategies used for preprocessing text before model building and similarity scoring

    Strategies must each accept a string and return a string
    """

    @staticmethod
    def _remove_stopwords(s, stopwords=CUSTOM_STOPWORDS):
        return " ".join(w for w in s.split() if w.lower() not in stopwords)

    @staticmethod
    def preprocess_gensim_v1(doc):
        preprocessing_filters = [
            unidecode,
            lambda x: x.lower(),
            strip_punctuation,
            strip_multiple_whitespaces,
            strip_numeric,
            partial(Preprocess._remove_stopwords, stopwords=GENSIM_STOPWORDS),
            Preprocess._remove_stopwords,
            strip_short,  # remove words shorter than 3 chars
            stem_text,  # This is the Porter stemmer
        ]

        return preprocess_string(doc, preprocessing_filters)

    @staticmethod
    def preprocess_gensim_v3(doc):
        preprocessed_v1 = Preprocess.preprocess_gensim_v1(doc)

        return preprocessed_v1 + Preprocess.bigrams(preprocessed_v1)

    @staticmethod
    def bigrams(list_of_words: list) -> list:
        """
        Turn a list of words into a list of bigrams
        """
        return [
            f"{list_of_words[i]} {list_of_words[i+1]}"
            for i in range(len(list_of_words) - 1)
        ]


class Strategy:
    """
    Defines strategies used for matching posts to plans

    Strategies must each accept a plans list and a post object and return a best_match dict

    Each strategy must adhere to the following contract

    :param plans: List of plan dicts
    :type plans: list of dict
    :param post_text: The text to interpret
    :type post: str
    :param threshold: Confidence threshold between 0-100. If confidence > threshold, then the plan is considered a match
    :type threshold: int
    :param \**kwargs: See below
    :return: {
        "match": plan id if the plan is considered a match, otherwise None
        "confidence": the confidence that the plan is a match (0 - 100)
        "plan": the best matching plan
        "potential_matches": [{plan_id, plan, confidence}] all potential matching plans, sorted from highest to lowest confidence
        # Can include other metadata about the match here
    }

    :Keyword Arguments:
      * *post* (`reddit_util.Comment/reddit_util.Submission`)
    """

    @staticmethod
    def token_sort_ratio(plans: list, post_text, threshold=50, **kwargs):
        """
        Match plans based on hardcoded plan topics, using fuzzywuzzy's token_sort_ratio for fuzzy matching
        """

        match_confidence = 0
        match = None

        for plan in plans:
            plan_match_confidence = fuzz.token_sort_ratio(
                post_text.lower(), plan["topic"].lower()
            )

            if plan_match_confidence > match_confidence:
                # Update match
                match_confidence = plan_match_confidence
                match = plan

        return {
            "match": match["id"] if match_confidence > threshold else None,
            "confidence": match_confidence,
            "plan": match,
        }

    @staticmethod
    # TODO allow thresholds
    def _composite_strategy(plans: list, post_text: str, strategies: list, **kwargs):
        """
        Run strategies in order until one has a match
        """
        for strategy in strategies:
            match_info = strategy(plans, post_text)
            if match_info["match"]:
                return match_info
        return match_info

    @staticmethod
    @lru_cache(maxsize=8)
    def _load_gensim_models(model_name, model, similarity, model_path):
        plan_ids = json.load(open(path.join(model_path, "plan_ids.json")))

        dictionary = corpora.Dictionary.load(path.join(model_path, "plans.dict"))

        index = similarity.load(path.join(model_path, f"{model_name}.index"))
        model = model.load(path.join(model_path, f"{model_name}.model"))

        return plan_ids, dictionary, index, model

    @staticmethod
    def _gensim_similarity(
        plans: list,
        post_text: str,
        model_name,
        model,
        similarity,
        threshold,
        potential_plan_threshold=50,
        model_path=GENSIM_V3_MODELS_PATH,
        preprocess=Preprocess.preprocess_gensim_v1,
        **kwargs,
    ):
        plan_ids, dictionary, index, model = Strategy._load_gensim_models(
            model_name, model, similarity, model_path
        )

        preprocessed_post = preprocess(post_text)

        vec_post = dictionary.doc2bow(preprocessed_post)

        # find similar plans
        sims = index[model[vec_post]]
        # sort by descending match
        sims = list(sorted(enumerate(sims), key=lambda item: -item[1]))

        potential_matches_with_dups = [
            {
                "plan_id": plan_ids[sim[0]],
                "plan": next(
                    filter(lambda p: p["id"] == plan_ids[sim[0]], plans), None
                ),
                "confidence": sim[1] * 100,
            }
            for sim in sims
        ]

        # dedupe potential matches
        potential_plan_ids = set()
        potential_matches = []
        for potential_match in potential_matches_with_dups:
            if potential_match["plan_id"] not in potential_plan_ids:
                potential_matches.append(potential_match)
                potential_plan_ids.add(potential_match["plan_id"])

        best_match_confidence = potential_matches[0]["confidence"]
        best_match_plan = potential_matches[0]["plan"]
        best_match_plan_id = potential_matches[0]["plan_id"]

        return {
            "match": best_match_plan_id if best_match_confidence > threshold else None,
            "confidence": best_match_confidence,
            "plan": best_match_plan,
            "potential_matches": list(
                filter(
                    lambda m: m["confidence"] > potential_plan_threshold,
                    potential_matches,
                )
            ),
        }

    @staticmethod
    def lsa_gensim_v3(plans: list, post_text: str, threshold=78.1, **kwargs):
        """
        LSI – Latent Semantic Indexing  (aka Latent Semantic Analysis)

        This version includes the hand-written topics from plans.json in the corpus
        of documents posts are matched against

        Models have been precomputed using ../scripts/update_gensim_models_v3.py
        """
        return Strategy._gensim_similarity(
            plans,
            post_text,
            "lsa",
            models.LsiModel,
            similarities.MatrixSimilarity,
            threshold,
            preprocess=Preprocess.preprocess_gensim_v3,
            model_path=GENSIM_V3_MODELS_PATH,
            **kwargs,
        )

    @staticmethod
    def tfidf_gensim_v3(plans: list, post_text: str, threshold=15, **kwargs):
        """
        TFIDF – Term Frequency–Inverse Document Frequency

        Using gensim

        Models have been precomputed using ../scripts/update_gensim_models_v3.py
        """
        return Strategy._gensim_similarity(
            plans,
            post_text,
            "tfidf",
            models.TfidfModel,
            similarities.MatrixSimilarity,
            threshold,
            model_path=GENSIM_V3_MODELS_PATH,
            preprocess=Preprocess.preprocess_gensim_v3,
            **kwargs,
        )


class RuleStrategy:
    """
    These are hardcoded matching rules that we want to make sure the bot respects, so that we can do human stuff
    for example, tell a user to say a phrase to get a certain plan

    They are otherwise similar to the functions in Strategy except that they don't accept a threshold argument,
    and they return None when there is no match
    """

    @staticmethod
    def match_verbatim(verbatims: list, post_text: str, options: set = set()):
        """
        Match exactly to a verbatim message's ID.
        """
        verbatim_id = None
        if "why_warren" in options or re.match(
            r"why warren\W*$", post_text, re.IGNORECASE | re.MULTILINE
        ):
            verbatim_id = "why_warren"
        else:
            match = re.match(
                r"(advanced\s+)?help\W*$", post_text, re.IGNORECASE | re.MULTILINE
            )
            if match:
                if match.group(1):
                    verbatim_id = "advanced_help"
                else:
                    verbatim_id = "basic_help"

        for verbatim in verbatims:
            if verbatim["id"] == verbatim_id:
                return {"operation": "verbatim", "verbatim": verbatim}

    @staticmethod
    def match_display_title(plans: list, post_text: str, **kwargs):
        """
        Exact display title matches. Include some preprocessing just to allow punctuation to be imperfect,
        or the user to include a stop word for some reason
        """
        preprocessed_post = Preprocess.preprocess_gensim_v1(post_text)
        for plan in plans:
            if (
                Preprocess.preprocess_gensim_v1(plan["display_title"])
                == preprocessed_post
            ):
                return {"match": plan["id"], "confidence": 100, "plan": plan}

    @staticmethod
    def request_plan_list(plans: list, post_text: str, **kwargs):
        """
        Matches strictly to a request at the end of the trigger line for the full list of all known plans
        """
        if re.search(r"show me the plans\W*$", post_text, re.IGNORECASE | re.MULTILINE):
            return {"operation": "all_the_plans"}

    @staticmethod
    def request_state_of_race(
        plans: list, post_text: str, options: set = set(), **kwargs
    ):
        """
        Matches strictly to a request at the end of the trigger line for the state of the race
        """
        if (
            "state_of_race" in options
            or re.search(
                r"state of (?:the )?(?:race|primary)\W*$",
                post_text,
                re.IGNORECASE | re.MULTILINE,
            )
            or re.search(
                r"is the (?:race|primary) over\W*$",
                post_text,
                re.IGNORECASE | re.MULTILINE,
            )
            or re.search(r"status check\W*$", post_text, re.IGNORECASE | re.MULTILINE)
        ):
            return {"operation": "state_of_race"}
