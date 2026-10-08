// Write the password-protected legacy .ppt fixture (../legacy-encrypted.ppt,
// #983) with Apache POI, the reader the indexer runs: one slide with a
// synthetic text box, the author and last-edit names set to a synthetic
// one (POI's blank-deck template carries a real name in its Current User
// stream), then encrypted with the given password (RC4 CryptoAPI).
// Run by generate-encrypted-ppt.sh inside the indexer's ppt-builder stage.
//
// Usage: java EncryptedPpt <output path> <password>
import java.awt.Rectangle;
import java.io.FileOutputStream;
import org.apache.poi.hslf.usermodel.HSLFSlide;
import org.apache.poi.hslf.usermodel.HSLFSlideShow;
import org.apache.poi.hslf.usermodel.HSLFTextBox;
import org.apache.poi.hssf.record.crypto.Biff8EncryptionKey;

public final class EncryptedPpt {
    private EncryptedPpt() {}

    public static void main(String[] args) throws Exception {
        try (HSLFSlideShow deck = new HSLFSlideShow()) {
            HSLFSlide slide = deck.createSlide();
            HSLFTextBox box = slide.createTextBox();
            box.setText("The SYNTHETIC-ENCRYPTED-DECK code is 6021.");
            box.setAnchor(new Rectangle(50, 50, 500, 100));
            deck.getSlideShowImpl().getCurrentUserAtom().setLastEditUsername("Synthetic Author");
            deck.getSlideShowImpl().createInformationProperties();
            deck.getSlideShowImpl().getSummaryInformation().setAuthor("Synthetic Author");
            deck.getSlideShowImpl().getSummaryInformation().setLastAuthor("Synthetic Author");
            Biff8EncryptionKey.setCurrentUserPassword(args[1]);
            try (FileOutputStream out = new FileOutputStream(args[0])) {
                deck.write(out);
            }
        } finally {
            Biff8EncryptionKey.setCurrentUserPassword(null);
        }
    }
}
