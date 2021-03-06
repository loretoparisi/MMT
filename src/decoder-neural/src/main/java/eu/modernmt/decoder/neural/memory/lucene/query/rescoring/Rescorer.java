package eu.modernmt.decoder.neural.memory.lucene.query.rescoring;

import eu.modernmt.decoder.neural.memory.ScoreEntry;
import eu.modernmt.lang.LanguagePair;
import eu.modernmt.model.ContextVector;
import eu.modernmt.model.Sentence;

/**
 * Created by davide on 06/08/17.
 */
public interface Rescorer {

    ScoreEntry[] rescore(LanguagePair direction, Sentence input, ScoreEntry[] entries, ContextVector context);

}
