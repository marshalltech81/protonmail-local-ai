// Print the text of a legacy .ppt with Apache POI HSLF (#957): slides and
// speaker notes; masters and comments are left out. Run by
// src/extractors/ppt.py, which bounds the JVM and reads stdout up to a
// byte cap. A password-protected deck (POI's encrypted-file exception,
// by exact class) ends the JVM with ENCRYPTED_EXIT_STATUS, which ppt.py
// records as a fixed "unsupported" row (#983). Any other error ends the
// JVM with a non-zero status (1 for an uncaught exception); its message
// goes to stderr, which the indexer discards because it can quote the
// deck.
import java.io.File;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import org.apache.poi.hslf.exceptions.EncryptedPowerPointFileException;
import org.apache.poi.hslf.usermodel.HSLFSlideShow;
import org.apache.poi.poifs.filesystem.POIFSFileSystem;
import org.apache.poi.sl.extractor.SlideShowExtractor;

public final class PptText {
    // Kept equal to ENCRYPTED_EXIT_STATUS in src/extractors/ppt.py by a test.
    static final int ENCRYPTED_EXIT_STATUS = 10;

    private PptText() {}

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(System.out, false, StandardCharsets.UTF_8);
        try (POIFSFileSystem fs = new POIFSFileSystem(new File(args[0]), true);
             HSLFSlideShow deck = new HSLFSlideShow(fs);
             SlideShowExtractor<?, ?> extractor = new SlideShowExtractor<>(deck)) {
            extractor.setNotesByDefault(true);
            out.print(extractor.getText());
        } catch (EncryptedPowerPointFileException e) {
            if (e.getClass() != EncryptedPowerPointFileException.class) {
                throw e;
            }
            System.exit(ENCRYPTED_EXIT_STATUS);
        }
        out.flush();
        if (out.checkError()) {
            System.exit(1);
        }
    }
}
