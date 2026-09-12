/* Synthetic test data authored for AI-DataSeek. No user or upstream sample data. */
import com.healthmarketscience.jackcess.*;
import java.io.File;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDateTime;
import java.util.*;

/** Reproducible content oracle: independent Jackcess writer AND read-only reader. */
public final class GenerateAccessFixtures {
  static void require(boolean condition, String message) {
    if (!condition) throw new AssertionError(message);
  }

  static Table samples(Database db) throws Exception {
    return new TableBuilder("Samples")
      .addColumn(new ColumnBuilder("ID", DataType.LONG))
      .addColumn(new ColumnBuilder("Label", DataType.TEXT).setLengthInUnits(128))
      .addColumn(new ColumnBuilder("Amount", DataType.MONEY))
      .addColumn(new ColumnBuilder("Exact", DataType.NUMERIC).setPrecision(28).setScale(4))
      .addColumn(new ColumnBuilder("Enabled", DataType.BOOLEAN))
      .addColumn(new ColumnBuilder("Observed", DataType.SHORT_DATE_TIME))
      .addColumn(new ColumnBuilder("Score", DataType.DOUBLE))
      .addColumn(new ColumnBuilder("Binary", DataType.BINARY).setLength(4))
      .toTable(db);
  }

  static void basic(Path path, Database.FileFormat format) throws Exception {
    require(!Files.exists(path), "Refusing to overwrite " + path);
    try (Database db = new DatabaseBuilder(path.toFile()).setFileFormat(format).create()) {
      db.setDateTimeType(DateTimeType.LOCAL_DATE_TIME);
      Table table = samples(db);
      table.addRow(1, "科学数据", new BigDecimal("1234.5678"),
        new BigDecimal("123456789012345678901234.5678"), true,
        LocalDateTime.of(2024, 2, 29, 12, 34, 56), 1.25, new byte[]{1, 2, 3, 4});
      table.addRow(2, "", new BigDecimal("-0.0001"),
        new BigDecimal("-9007199254740993.0001"), false,
        LocalDateTime.of(2000, 1, 1, 0, 0, 0), -2.5, null);
      table.addRow(3, null, null, null, true, null, null, null);
      table.addRow(4, "<script>inert</script>", new BigDecimal("922337203685477.5807"),
        new BigDecimal("0.0000"), false,
        LocalDateTime.of(2026, 9, 11, 0, 0, 0), 0.0, new byte[]{0, 0, 0, 0});
      new TableBuilder("Empty").addColumn(new ColumnBuilder("Value", DataType.TEXT)).toTable(db);
    }
    try (Database db = new DatabaseBuilder(path.toFile()).setReadOnly(true).open()) {
      db.setDateTimeType(DateTimeType.LOCAL_DATE_TIME);
      List<Row> rows = new ArrayList<>();
      db.getTable("Samples").forEach(rows::add);
      require(rows.size() == 4, "row count");
      require("科学数据".equals(rows.get(0).get("Label")), "Chinese text");
      require("".equals(rows.get(1).get("Label")), "empty text preserved");
      require(rows.get(2).get("Label") == null, "NULL distinct from empty");
      require(new BigDecimal("123456789012345678901234.5678").equals(rows.get(0).get("Exact")), "decimal precision");
      require(new BigDecimal("922337203685477.5807").equals(rows.get(3).get("Amount")), "money maximum");
      require(LocalDateTime.of(2024, 2, 29, 12, 34, 56).equals(rows.get(0).get("Observed")), "date semantics");
      require(Boolean.FALSE.equals(rows.get(1).get("Enabled")), "boolean false");
      require(db.getTable("Empty").getRowCount() == 0, "empty table");
      System.out.println(path.getFileName() + ": independent read-only oracle passed");
    }
  }

  static void special(Path path, Database.FileFormat format, boolean linked) throws Exception {
    require(!Files.exists(path), "Refusing to overwrite " + path);
    try (Database db = new DatabaseBuilder(path.toFile()).setFileFormat(format).create()) {
      if (linked) {
        // Only a synthetic link marker is written; the target is never opened.
        db.createLinkedTable("ExternalTable", "/not-mounted/dataseek-test-only.mdb", "Samples");
      } else {
        Table table = new TableBuilder("Unsupported")
          .addColumn(new ColumnBuilder("ID", DataType.LONG))
          .addColumn(new ColumnBuilder("Note", DataType.MEMO))
          .addColumn(new ColumnBuilder("Object", DataType.OLE))
          .toTable(db);
        table.addRow(1, "Synthetic memo, not executed.", new byte[]{1, 2, 3, 4});
      }
    }
    System.out.println(path.getFileName() + ": synthetic boundary fixture generated");
  }

  public static void main(String[] args) throws Exception {
    require(args.length == 1, "Usage: GenerateAccessFixtures OUTPUT_DIRECTORY");
    Path dir = Path.of(args[0]);
    Files.createDirectories(dir);
    basic(dir.resolve("synthetic-v2000.mdb"), Database.FileFormat.V2000);
    basic(dir.resolve("synthetic-v2010.accdb"), Database.FileFormat.V2010);
    special(dir.resolve("synthetic-linked-v2000.mdb"), Database.FileFormat.V2000, true);
    special(dir.resolve("synthetic-memo-ole-v2010.accdb"), Database.FileFormat.V2010, false);
  }
}
