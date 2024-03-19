import os

import bittensor as bt
from dotenv import load_dotenv

from ..protocol import SortType

from .ranking.recency_ranking import RecencyRankingModel


class StructuredSearchEngine:
    def __init__(
        self,
        search_client,
        relevance_ranking_model,
        twitter_crawler=None,
        recall_size=50,
    ):
        load_dotenv()

        self.search_client = search_client
        self.init_indices()

        # for relevance ranking recalled results
        self.relevance_ranking_model = relevance_ranking_model

        self.recency_ranking_model = RecencyRankingModel()

        self.recall_size = recall_size

        # optional, for crawling data
        self.twitter_crawler = twitter_crawler

    def twitter_doc_mapper(cls, doc):
        return {
            "id": doc["id"],
            "text": doc["text"],
            "created_at": doc["created_at"],
            "username": doc["username"],
            "url": doc["url"],
            "quote_count": doc["quote_count"],
            "reply_count": doc["reply_count"],
            "retweet_count": doc["retweet_count"],
            "favorite_count": doc["favorite_count"],
        }

    def init_indices(self):
        """
        Initializes the indices in the elasticsearch database.
        """
        index_name = "twitter"
        if not self.search_client.indices.exists(index=index_name):
            bt.logging.info("creating index...", index_name)
            self.search_client.indices.create(
                index=index_name,
                body={
                    "mappings": {
                        "properties": {
                            "id": {"type": "long"},
                            "text": {"type": "text"},
                            "created_at": {"type": "date"},
                            "username": {"type": "keyword"},
                            "url": {"type": "text"},
                            "quote_count": {"type": "long"},
                            "reply_count": {"type": "long"},
                            "retweet_count": {"type": "long"},
                            "favorite_count": {"type": "long"},
                        }
                    }
                },
            )

    def search(self, search_query):
        """
        Structured search interface for this search engine

        Args:
        - search_query: A `StructuredSearchSynapse` or `SearchSynapse` object representing the search request sent by the validator.
        """

        result_size = search_query.size

        recalled_items = self.recall(
            search_query=search_query, recall_size=self.recall_size
        )

        # default ranking model
        ranking_model = self.relevance_ranking_model

        # use recency ranking model if the search query is for recency
        if (
            search_query.name == "StructuredSearchSynapse"
            and search_query.sort_type == SortType.RECENCY
        ):
            ranking_model = self.recency_ranking_model

        results = ranking_model.rank(search_query.query_string, recalled_items)

        return results[:result_size]

    def recall(self, search_query, recall_size):
        """
        Structured recall interface for this search engine
        """
        query_string = search_query.query_string

        es_query = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "query_string": {
                                "query": query_string,
                                "default_field": "text",
                                "default_operator": "AND",
                            }
                        }
                    ],
                }
            },
            "size": recall_size,
        }

        if search_query.name == "StructuredSearchSynapse":
            time_filter = {}
            if search_query.earlier_than_timestamp:
                time_filter["lte"] = search_query.earlier_than_timestamp
            if search_query.later_than_timestamp:
                time_filter["gte"] = search_query.later_than_timestamp
            if time_filter:
                es_query["query"]["bool"]["must"].append(
                    {"range": {"created_at": time_filter}}
                )

            if search_query.sort_type == SortType.RECENCY:
                es_query["sort"] = [{"created_at": "desc"}]

        bt.logging.debug(f"es_query: {es_query}")

        try:
            response = self.search_client.search(
                index="twitter",
                body=es_query,
            )
            documents = response["hits"]["hits"]
            results = []
            for document in documents if documents else []:
                doc = document["_source"]
                results.append(self.twitter_doc_mapper(doc))
            bt.logging.info(f"retrieved {len(results)} results")
            bt.logging.trace(f"results: ")
            return results
        except Exception as e:
            bt.logging.error("recall error...", e)
            return []

    def crawl_and_index_data(self, query_string, max_size):
        """
        Crawls the data from the twitter crawler and indexes it in the elasticsearch database.
        """
        if self.twitter_crawler is None:
            bt.logging.warning(
                "Twitter crawler is not initialized. skipped crawling and indexing"
            )
        try:
            processed_docs = self.twitter_crawler.search(query_string, max_size)
            bt.logging.debug(f"crawled {len(processed_docs)} docs")
            bt.logging.trace(processed_docs)
        except Exception as e:
            bt.logging.error("crawling error...", e)
            processed_docs = []

        if len(processed_docs) > 0:
            try:
                bt.logging.info(f"bulk indexing {len(processed_docs)} docs")
                bulk_body = []
                for doc in processed_docs:
                    bulk_body.append(
                        {
                            "update": {
                                "_index": "twitter",
                                "_id": doc["id"],
                            }
                        }
                    )
                    bulk_body.append(
                        {
                            "doc": doc,
                            "doc_as_upsert": True,
                        }
                    )

                r = self.search_client.bulk(
                    body=bulk_body,
                    refresh=True,
                )
                bt.logging.trace("bulk update response...", r)
                if not r.get("errors"):
                    bt.logging.info("bulk update succeeded")
                else:
                    bt.logging.error("bulk update failed: ", r)
            except Exception as e:
                bt.logging.error("bulk update error...", e)