import logging
import time
from statistics import mean
from typing import Type

import numpy as np
from scipy.special import expit

from haystack.reader.base import BaseReader
from haystack.retriever.base import BaseRetriever

logger = logging.getLogger(__name__)


class Finder:
    """
    Finder ties together instances of the Reader and Retriever class.

    It provides an interface to predict top n answers for a given question.
    """

    def __init__(self, reader: Type[BaseReader], retriever: Type[BaseRetriever]):
        self.retriever = retriever
        self.reader = reader
        if self.reader is None and self.retriever is None:
            raise AttributeError("Finder: self.reader and self.retriever can not be both None")

    def get_answers(self, question: str, top_k_reader: int = 1, top_k_retriever: int = 10, filters: dict = None):
        """
        Get top k answers for a given question.

        :param question: the question string
        :param top_k_reader: number of answers returned by the reader
        :param top_k_retriever: number of text units to be retrieved
        :param filters: limit scope to documents having the given tags and their corresponding values.
            The format for the dict is {"tag-1": ["value-1","value-2"], "tag-2": ["value-3]" ...}
        :return:
        """

        if self.retriever is None or self.reader is None:
            raise AttributeError("Finder.get_answers_via_similar_questions requires self.retriever AND self.reader")

        # 1) Apply retriever(with optional filters) to get fast candidate documents
        documents = self.retriever.retrieve(question, filters=filters, top_k=top_k_retriever)

        if len(documents) == 0:
            logger.info("Retriever did not return any documents. Skipping reader ...")
            results = {"question": question, "answers": []}
            return results

        # 2) Apply reader to get granular answer(s)
        len_chars = sum([len(d.text) for d in documents])
        logger.info(f"Reader is looking for detailed answer in {len_chars} chars ...")

        results = self.reader.predict(question=question,
                                      documents=documents,
                                      top_k=top_k_reader)

        # Add corresponding document_name and more meta data, if an answer contains the document_id
        for ans in results["answers"]:
            ans["meta"] = {}
            for doc in documents:
                if doc.id == ans["document_id"]:
                    ans["meta"] = doc.meta

        return results

    def get_answers_via_similar_questions(self, question: str, top_k_retriever: int = 10, filters: dict = None):
        """
        Get top k answers for a given question using only a retriever.

        :param question: the question string
        :param top_k_retriever: number of text units to be retrieved
        :param filters: limit scope to documents having the given tags and their corresponding values.
            The format for the dict is {"tag-1": "value-1", "tag-2": "value-2" ...}
        :return:
        """

        if self.retriever is None:
            raise AttributeError("Finder.get_answers_via_similar_questions requires self.retriever")

        results = {"question": question, "answers": []}

        # 1) Optional: reduce the search space via document tags
        if filters:
            logging.info(f"Apply filters: {filters}")
            candidate_doc_ids = self.retriever.document_store.get_document_ids_by_tags(filters)
            logger.info(f"Got candidate IDs due to filters:  {candidate_doc_ids}")

            if len(candidate_doc_ids) == 0:
                # We didn't find any doc matching the filters
                return results

        else:
            candidate_doc_ids = None

        # 2) Apply retriever to match similar questions via cosine similarity of embeddings
        documents = self.retriever.retrieve(question, top_k=top_k_retriever, candidate_doc_ids=candidate_doc_ids)

        # 3) Format response
        for doc in documents:
            #TODO proper calibratation of pseudo probabilities
            cur_answer = {"question": doc.meta["question"], "answer": doc.text, "context": doc.text,
                          "score": doc.query_score, "offset_start": 0, "offset_end": len(doc.text), "meta": doc.meta
                          }
            if self.retriever.embedding_model:
                probability = (doc.query_score + 1) / 2
            else:
                probability = float(expit(np.asarray(doc.query_score / 8)))
            cur_answer["probability"] = probability
            results["answers"].append(cur_answer)

        return results

    def eval(
        self,
        label_index: str = "feedback",
        doc_index: str = "eval_document",
        label_origin: str = "gold_label",
        top_k_retriever: int = 10,
        top_k_reader: int = 10,
    ):
        """
        Evaluation of the whole pipeline by first evaluating the Retriever and then evaluating the Reader on the result
        of the Retriever.

        Returns a dict containing the following metrics:
            - "retriever_recall": Proportion of questions for which correct document is among retrieved documents
            - "retriever_map": Mean of average precision for each question. Rewards retrievers that give relevant
              documents a higher rank.
            - "reader_top1_accuracy": Proportion of highest ranked predicted answers that overlap with corresponding correct answer
            - "reader_top1_accuracy_has_answer": Proportion of highest ranked predicted answers that overlap
                                                 with corresponding correct answer for answerable questions
            - "reader_top_k_accuracy": Proportion of predicted answers that overlap with corresponding correct answer
            - "reader_topk_accuracy_has_answer": Proportion of predicted answers that overlap with corresponding correct answer
                                                 for answerable questions
            - "reader_top1_em": Proportion of exact matches of highest ranked predicted answers with their corresponding
                                correct answers
            - "reader_top1_em_has_answer": Proportion of exact matches of highest ranked predicted answers with their corresponding
                                           correct answers for answerable questions
            - "reader_topk_em": Proportion of exact matches of predicted answers with their corresponding correct answers
            - "reader_topk_em_has_answer": Proportion of exact matches of predicted answers with their corresponding
                                           correct answers for answerable questions
            - "reader_top1_f1": Average overlap between highest ranked predicted answers and their corresponding correct answers
            - "reader_top1_f1_has_answer": Average overlap between highest ranked predicted answers and their corresponding
                                           correct answers for answerable questions
            - "reader_topk_f1": Average overlap between predicted answers and their corresponding correct answers
            - "reader_topk_f1_has_answer": Average overlap between predicted answers and their corresponding correct answers
                                           for answerable questions
            - "reader_top1_no_answer_accuracy": Proportion of correct predicting unanswerable question at highest ranked prediction
            - "reader_topk_no_answer_accuracy": Proportion of correct predicting unanswerable question among all predictions
            - "total_retrieve_time": Time retriever needed to retrieve documents for all questions
            - "avg_retrieve_time": Average time needed to retrieve documents for one question
            - "total_reader_time": Time reader needed to extract answer out of retrieved documents for all questions
                                   where the correct document is among the retrieved ones
            - "avg_reader_time": Average time needed to extract answer out of retrieved documents for one question
            - "total_finder_time": Total time for whole pipeline

        :param label_index: Elasticsearch index where labeled questions are stored
        :type label_index: str
        :param doc_index: Elasticsearch index where documents that are used for evaluation are stored
        :type doc_index: str
        :param top_k_retriever: How many documents per question to return and pass to reader
        :type top_k_retriever: int
        :param top_k_reader: How many answers to return per question
        :type top_k_reader: int
        """
        finder_start_time = time.time()
        # extract all questions for evaluation
        filter = {"origin": label_origin}
        questions = self.retriever.document_store.get_all_documents_in_index(index=label_index, filters=filter)

        correct_retrievals = 0
        summed_avg_precision_retriever = 0
        retrieve_times = []

        correct_readings_top1 = 0
        correct_readings_topk = 0
        correct_readings_top1_has_answer = 0
        correct_readings_topk_has_answer = 0
        exact_matches_top1 = 0
        exact_matches_topk = 0
        exact_matches_top1_has_answer = 0
        exact_matches_topk_has_answer = 0
        summed_f1_top1 = 0
        summed_f1_topk = 0
        summed_f1_top1_has_answer = 0
        summed_f1_topk_has_answer = 0
        correct_no_answers_top1 = 0
        correct_no_answers_topk =  0
        read_times = []

        # retrieve documents
        questions_with_docs = []
        retriever_start_time = time.time()
        for q_idx, question in enumerate(questions):
            question_string = question["_source"]["question"]
            single_retrieve_start = time.time()
            retrieved_docs = self.retriever.retrieve(question_string, top_k=top_k_retriever, index=doc_index)
            retrieve_times.append(time.time() - single_retrieve_start)
            for doc_idx, doc in enumerate(retrieved_docs):
                # check if correct doc among retrieved docs
                if doc.meta["doc_id"] == question["_source"]["doc_id"]:
                    correct_retrievals += 1
                    summed_avg_precision_retriever += 1 / (doc_idx + 1)
                    questions_with_docs.append({
                        "question": question,
                        "docs": retrieved_docs,
                        "correct_es_doc_id": doc.id})
                    break
        retriever_total_time = time.time() - retriever_start_time
        number_of_questions = q_idx + 1

        number_of_no_answer = 0
        previous_return_no_answers = self.reader.return_no_answers
        self.reader.return_no_answers = True
        # extract answers
        reader_start_time = time.time()
        for q_idx, question in enumerate(questions_with_docs):
            if (q_idx + 1) % 100 == 0:
                print(f"Processed {q_idx+1} questions.")
            question_string = question["question"]["_source"]["question"]
            docs = question["docs"]
            single_reader_start = time.time()
            predicted_answers = self.reader.predict(question_string, docs, top_k_reader)
            read_times.append(time.time() - single_reader_start)
            # check if question is answerable
            if question["question"]["_source"]["answers"]:
                for answer_idx, answer in enumerate(predicted_answers["answers"]):
                    found_answer = False
                    found_em = False
                    best_f1 = 0
                    # check if correct document
                    if answer["document_id"] == question["correct_es_doc_id"]:
                        gold_spans = [(gold_answer["answer_start"], gold_answer["answer_start"] + len(gold_answer["text"]) + 1)
                                      for gold_answer in question["question"]["_source"]["answers"]]
                        predicted_span = (answer["offset_start_in_doc"], answer["offset_end_in_doc"])

                        for gold_span in gold_spans:
                            # check if overlap between gold answer and predicted answer
                            # top-1 answer
                            if not found_answer:
                                if (gold_span[0] <= predicted_span[1]) and (predicted_span[0] <= gold_span[1]):
                                    # top-1 answer
                                    if answer_idx == 0:
                                        correct_readings_top1 += 1
                                        correct_readings_top1_has_answer += 1
                                    # top-k answers
                                    correct_readings_topk += 1
                                    correct_readings_topk_has_answer += 1
                                    found_answer = True
                            # check for exact match
                            if not found_em:
                                if (gold_span[0] == predicted_span[0]) and (gold_span[1] == predicted_span[1]):
                                    # top-1-answer
                                    if answer_idx == 0:
                                        exact_matches_top1 += 1
                                        exact_matches_top1_has_answer += 1
                                    # top-k answers
                                    exact_matches_topk += 1
                                    exact_matches_topk_has_answer += 1
                                    found_em = True
                            # calculate f1
                            pred_indices = list(range(predicted_span[0], predicted_span[1] + 1))
                            gold_indices = list(range(gold_span[0], gold_span[1] + 1))
                            n_overlap = len([x for x in pred_indices if x in gold_indices])
                            if pred_indices and gold_indices and n_overlap:
                                precision = n_overlap / len(pred_indices)
                                recall = n_overlap / len(gold_indices)
                                current_f1 = (2 * precision * recall) / (precision + recall)
                                # top-1 answer
                                if answer_idx == 0:
                                    summed_f1_top1 += current_f1
                                    summed_f1_top1_has_answer += current_f1
                                if current_f1 > best_f1:
                                    best_f1 = current_f1
                        # top-k answers: use best f1-score
                        summed_f1_topk += best_f1
                        summed_f1_topk_has_answer += best_f1

                    if found_answer and found_em:
                        break
            # question not answerable
            else:
                number_of_no_answer += 1
                # As question is not answerable, it is not clear how to compute average precision for this question.
                # For now, we decided to calculate average precision based on the rank of 'no answer'.
                for answer_idx, answer in enumerate(predicted_answers["answers"]):
                    # check if 'no answer'
                    if answer["answer"] is None:
                        if answer_idx == 0:
                            correct_no_answers_top1 += 1
                            correct_readings_top1 += 1
                            exact_matches_top1 += 1
                            summed_f1_top1 += 1
                        correct_no_answers_topk += 1
                        correct_readings_topk += 1
                        exact_matches_topk += 1
                        summed_f1_topk += 1
                        break
        number_of_has_answer = correct_retrievals - number_of_no_answer

        reader_total_time = time.time() - reader_start_time
        finder_total_time = time.time() - finder_start_time

        retriever_recall = correct_retrievals / number_of_questions
        retriever_map = summed_avg_precision_retriever / number_of_questions

        reader_top1_accuracy = correct_readings_top1 / correct_retrievals
        reader_top1_accuracy_has_answer = correct_readings_top1_has_answer / number_of_has_answer
        reader_top_k_accuracy = correct_readings_topk / correct_retrievals
        reader_topk_accuracy_has_answer = correct_readings_topk_has_answer / number_of_has_answer
        reader_top1_em = exact_matches_top1 / correct_retrievals
        reader_top1_em_has_answer = exact_matches_top1_has_answer / number_of_has_answer
        reader_topk_em = exact_matches_topk / correct_retrievals
        reader_topk_em_has_answer = exact_matches_topk_has_answer / number_of_has_answer
        reader_top1_f1 = summed_f1_top1 / correct_retrievals
        reader_top1_f1_has_answer = summed_f1_top1_has_answer / number_of_has_answer
        reader_topk_f1 = summed_f1_topk / correct_retrievals
        reader_topk_f1_has_answer = summed_f1_topk_has_answer / number_of_has_answer
        reader_top1_no_answer_accuracy = correct_no_answers_top1 / number_of_no_answer
        reader_topk_no_answer_accuracy = correct_no_answers_topk / number_of_no_answer

        self.reader.return_no_answers = previous_return_no_answers

        logger.info((f"{correct_readings_topk} out of {number_of_questions} questions were correctly answered "
                     f"({(correct_readings_topk/number_of_questions):.2%})."))
        logger.info(f"{number_of_questions-correct_retrievals} questions could not be answered due to the retriever.")
        logger.info(f"{correct_retrievals-correct_readings_topk} questions could not be answered due to the reader.")

        results = {
            "retriever_recall": retriever_recall,
            "retriever_map": retriever_map,
            "reader_top1_accuracy": reader_top1_accuracy,
            "reader_top1_accuracy_has_answer": reader_top1_accuracy_has_answer,
            "reader_top_k_accuracy": reader_top_k_accuracy,
            "reader_topk_accuracy_has_answer": reader_topk_accuracy_has_answer,
            "reader_top1_em": reader_top1_em,
            "reader_top1_em_has_answer": reader_top1_em_has_answer,
            "reader_topk_em": reader_topk_em,
            "reader_topk_em_has_answer": reader_topk_em_has_answer,
            "reader_top1_f1": reader_top1_f1,
            "reader_top1_f1_has_answer": reader_top1_f1_has_answer,
            "reader_topk_f1": reader_topk_f1,
            "reader_topk_f1_has_answer": reader_topk_f1_has_answer,
            "reader_top1_no_answer_accuracy": reader_top1_no_answer_accuracy,
            "reader_topk_no_answer_accuracy": reader_topk_no_answer_accuracy,
            "total_retrieve_time": retriever_total_time,
            "avg_retrieve_time": mean(retrieve_times),
            "total_reader_time": reader_total_time,
            "avg_reader_time": mean(read_times),
            "total_finder_time": finder_total_time
        }

        return results