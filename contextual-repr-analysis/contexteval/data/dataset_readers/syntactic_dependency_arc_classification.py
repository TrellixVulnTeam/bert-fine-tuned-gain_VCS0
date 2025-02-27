from typing import Dict, List, Optional, Set, Tuple, Union
import logging

from allennlp.common.file_utils import cached_path
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.fields import ArrayField, Field, ListField, MetadataField, SequenceLabelField
from allennlp.data.instance import Instance
from conllu.parser import parse_line, DEFAULT_FIELDS
from overrides import overrides
import numpy as np
from torch import FloatTensor

from contexteval.contextualizers import Contextualizer
from contexteval.data.dataset_readers import TruncatableDatasetReader
from contexteval.data.fields import SequenceArrayField

logger = logging.getLogger(__name__)


def lazy_parse(text: str, fields: Tuple = DEFAULT_FIELDS):
    for sentence in text.split("\n\n"):
        if sentence:
            annotations = [
                parse_line(line, fields) for line in sentence.split("\n")
                if line and not line.strip().startswith("#")]

            # (child, parent/head) pairs
            arc_indices = []
            # Strings with the relation for each pair
            arc_labels = []
            # print(sentence)
            for idx, annotation in enumerate(annotations):
                head = annotation["head"]
                # print(idx, head)
                if head == 0:
                    # Skip the root
                    continue
                if not head:
                    # Skip the None Type
                    continue
                # UD marks the head with 1-indexed numbering, so we subtract
                # one to get the index of the parent.
                arc_indices.append((idx, head - 1))
                arc_labels.append(annotation["deprel"])
            yield annotations, arc_indices, arc_labels


@DatasetReader.register("syntactic_dependency_arc_classification")
class SyntacticDependencyArcClassificationDatasetReader(TruncatableDatasetReader):
    """
    Given the representations of two words, a model is tasked to identify the
    label of the syntactic dependency going from word 1 to word 2.

    Parameters
    ----------
    directed: boolean, optional (default=``True``)
        If ``True``, the examples are created for specific head->child relations.
        If ``False``, both the head->child relation and the child->head relation
        are used as training examples (with the same label).
    include_raw_tokens: bool, optional (default=``False``)
        Whether to include the raw tokens in the generated instances. This is
        False by default since it's slow, but it is necessary if you want to use
        your ``Contextualizer`` as part of a model (e.g., for finetuning).
    contextualizer: Contextualizer, optional (default=``None``)
        If provided, it is used to produce contextualized representations of the text.
    max_instances: int or float, optional (default=``None``)
        The number of instances to use during training. If int, this value is taken
        to be the absolute amount of instances to use. If float, this value indicates
        that we should use that proportion of the total training data. If ``None``,
        all instances are used.
    seed: int, optional (default=``0``)
        The random seed to use.
    lazy : ``bool``, optional (default=``False``)
        If this is true, ``instances()`` will return an object whose ``__iter__`` method
        reloads the dataset each time it's called. Otherwise, ``instances()`` returns a list.
    """
    def __init__(self,
                 directed: bool = True,
                 include_raw_tokens: bool = False,
                 contextualizer: Contextualizer = None,
                 max_instances: Union[int, float] = None,
                 seed: int = 0,
                 lazy: bool = False) -> None:
        super().__init__(max_instances=max_instances,
                         seed=seed,
                         lazy=lazy)
        self._directed = directed
        self._contextualizer = contextualizer
        self._include_raw_tokens = include_raw_tokens

    @overrides
    def _read_dataset(self,
                      file_path: str,
                      count_only: bool = False,
                      keep_idx: Optional[Set[int]] = None):
        """
        Yield instances from the file_path.

        Parameters
        ----------
        file_path: str, required
            The path to the data file.
        count_only: bool, optional (default=``False``)
            If True, no instances are returned and instead a dummy object is
            returned. This is useful for quickly counting the number of instances
            in the data file, since creating instances is relatively expensive.
        keep_idx: Set[int], optional (default=``None``)
            If not None, only yield instances whose index is in this set.
        """
        file_path = cached_path(file_path)

        if count_only:
            logger.info("Counting syntactic dependency arc prediction instances "
                        "in CoNLL-U formatted dataset at: %s", file_path)
        else:
            logger.info("Reading syntactic dependency arc prediction data "
                        "from CoNLL-U formatted dataset at: %s", file_path)
        index = 0
        with open(file_path, 'r') as conllu_file:
            for annotation, directed_arc_indices, arc_labels in lazy_parse(conllu_file.read()):
                if self._directed is False:
                    # Undirected mode, augment the directed_arc_indices with undirected pairs.
                    undirected_arc_indices = []
                    undirected_arc_labels = []
                    directed_arc_indices_set = set(directed_arc_indices)
                    for directed_arc, label in zip(directed_arc_indices, arc_labels):
                        undirected_arc_indices.append(directed_arc)
                        undirected_arc_labels.append(label)
                        if (directed_arc[1], directed_arc[0]) not in directed_arc_indices_set:
                            undirected_arc_indices.append((directed_arc[1], directed_arc[0]))
                            undirected_arc_labels.append(label)
                    all_arc_indices = undirected_arc_indices
                    all_arc_labels = undirected_arc_labels
                else:
                    all_arc_labels = arc_labels
                    all_arc_indices = directed_arc_indices

                # If there are no arc indices, then this sentence does not produce any Instances
                # and we should thus skip it.
                if not all_arc_indices:
                    continue

                if keep_idx is not None and index not in keep_idx:
                    index += 1
                    continue
                if count_only:
                    yield 1
                    continue

                # Get the tokens in the sentence and contextualize them, storing the results.
                tokens = [x["form"] for x in annotation]
                # Contextualize the tokens if a Contextualizer was provided.
                # TODO (nfliu): How can we make this batched?
                # Would make contextualizers that use the GPU much faster.
                if self._contextualizer:
                    token_representations = self._contextualizer([tokens])[0]
                else:
                    token_representations = None

                # Iterate over each of the (directed or undirected) arc_indices
                yield self.text_to_instance(
                    tokens=tokens,
                    arc_indices=all_arc_indices,
                    token_representations=token_representations,
                    labels=all_arc_labels)
                index += 1

    def text_to_instance(self,  # type: ignore
                         tokens: List[str],
                         arc_indices: List[Tuple[int, int]],
                         token_representations: FloatTensor = None,
                         labels: List[str] = None):
        """
        Parameters
        ----------
        tokens : ``List[str]``, required.
            The tokens in the sentence to be encoded.
        arc_indices: ``List[Tuple[int, int]]``, required.
            A List of tuples, where each item denotes an arc. An arc is a
            tuple of (child index, parent index). Indices are 0 indexed.
        token_representations: ``FloatTensor``, optional (default=``None``)
            Precomputed token representations to use in the instance. If ``None``,
            we use a ``Contextualizer`` provided to the dataset reader to calculate
            the token representations. Shape is (seq_len, representation_dim).
        labels: ``List[str]``, optional (default=``None``)
            The labels of the arcs. ``None`` indicates that labels are not
            provided.

        Returns
        -------
        An ``Instance`` containing the following fields:
            raw_tokens : ListField[MetadataField]
                The raw str tokens in the sequence. Each MetadataField stores the raw string
                of a single token.
            arc_indices : ``SequenceArrayField``
                Array of shape (num_arc_indices, 2) corresponding to the arc indices.
                The first column holds the child indices, and the 2nd column holds
                their respective parent indices.
            token_representations: ``ArrayField``
                Contains the representation of the tokens.
            labels: ``SequenceLabelField``
                The labels corresponding each arc represented in token_indices.
        """
        # TODO(nfliu): consolidate this with
        # SemanticDependencyArcClassificationDatasetReader
        fields: Dict[str, Field] = {}

        # Add tokens to the field
        if self._include_raw_tokens:
            fields["raw_tokens"] = ListField([MetadataField(token) for token in tokens])
        # Add arc indices to the field
        arc_indices_field = SequenceArrayField(
            np.array(arc_indices, dtype="int64"))
        fields["arc_indices"] = arc_indices_field

        if token_representations is None and self._contextualizer:
            # Contextualize the tokens
            token_representations = self._contextualizer([tokens])[0]

        # Add representations of the tokens at the arc indices to the field
        # If we don't have representations, use an empty numpy array.
        if token_representations is not None:
            fields["token_representations"] = ArrayField(
                token_representations.numpy())
        if labels:
            fields["labels"] = SequenceLabelField(labels, arc_indices_field)
        return Instance(fields)
