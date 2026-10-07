// Print the text of a legacy .ppt with Apache POI HSLF (#957): slides and
// speaker notes; masters and comments are left out. Run by
// src/extractors/ppt.py, which bounds the JVM and reads stdout up to a
// byte cap. Any error ends the JVM with a non-zero status; its message
// goes to stderr, which the indexer discards because it can quote the
// deck.
import java.io.File;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import org.apache.poi.hslf.usermodel.HSLFSlideShow;
import org.apache.poi.poifs.filesystem.POIFSFileSystem;
import org.apache.poi.sl.extractor.SlideShowExtractor;

public final class PptText {
    private PptText() {}

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(System.out, false, StandardCharsets.UTF_8);
        try (POIFSFileSystem fs = new POIFSFileSystem(new File(args[0]), true);
             HSLFSlideShow deck = new HSLFSlideShow(fs);
             SlideShowExtractor<?, ?> extractor = new SlideShowExtractor<>(deck)) {
            extractor.setNotesByDefault(true);
            out.print(extractor.getText());
        }
        out.flush();
        if (out.checkError()) {
            System.exit(1);
        }
    }
}
