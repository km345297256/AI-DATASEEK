/* DataSeek's narrow read-only adapter to the public libmdb 1.0.1 ABI.
 * No SQL engine, mdb_read_catalog(), property blobs, macros or linked tables.
 * Build: cc -O2 -Wall -Wextra access_table_native.c $(pkg-config --cflags --libs libmdb) -lm
 * The isolated worker supplies only a fixed private snapshot and integer IDs.
 */
#include <mdbtools.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <sys/resource.h>

#define MAX_OUTPUT (2 * 1024 * 1024)
#define DB_INPUT_LIMIT (16 * 1024 * 1024)
#define MAX_TABLES 32
#define MAX_COLS 128

static GString *output;
static void need(int ok) { if (!ok) { fputs("Access preview rejected.\n", stderr); exit(2); } }
static void emit(const char *s) { need(strlen(s) <= MAX_OUTPUT - output->len); g_string_append(output, s); }
static void number(long long n) { char b[32]; snprintf(b, sizeof b, "%lld", n); emit(b); }
static void quoted(const char *s, size_t len) {
    need(len <= 16384);
    emit("\"");
    for (size_t i = 0; i < len; i++) {
        unsigned char c = s[i];
        if (c == '"') emit("\\\"");
        else if (c == '\\') emit("\\\\");
        else if (c < 32) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); emit(b); }
        else { char b[2] = { (char)c, 0 }; emit(b); }
    }
    emit("\"");
}
static void string(const char *s) { need(s != NULL); quoted(s, strlen(s)); }
static unsigned parse_uint(const char *s, unsigned max) {
    need(s && *s && strlen(s) <= 6); unsigned n = 0;
    for (; *s; s++) { need(*s >= '0' && *s <= '9'); n = n * 10 + (*s - '0'); need(n <= max); }
    return n;
}

/* libmdb normally replaces invalid UCS-2 with '?'. Validate the same bounded
 * Unicode-compression envelope first so corruption is not silently repaired.
 * Jet 3 codepage variants are intentionally not enabled in this first reader.
 */
static void valid_ucs2(const unsigned char *src, size_t len) {
    int compressed = len >= 2 && src[0] == 0xff && src[1] == 0xfe;
    int mode = compressed;
    for (size_t p = compressed ? 2 : 0; p < len;) {
        if (compressed && src[p] == 0) { mode = !mode; p++; continue; }
        if (mode) { p++; continue; }
        need(p + 1 < len);
        unsigned c = src[p] | (src[p + 1] << 8);
        need(c < 0xd800 || c > 0xdfff);  /* libmdb 1.0.1 uses UCS-2, not UTF-16. */
        p += 2;
    }
}
static char *text_value(MdbHandle *mdb, MdbField *field, size_t *length) {
    need(field->siz >= 0 && field->siz <= 4096);
    const unsigned char *src = mdb->pg_buf + field->start;
    valid_ucs2(src, field->siz);
    char *dest = g_malloc0(16384);
    int size = mdb_unicode2ascii(mdb, (const char *)src, field->siz, dest, 16384);
    need(size >= 0 && size < 16383);
    /* Validate each NUL-separated span: JSON retains NUL for Python's explicit
     * unsafe-text marker instead of truncating a value at the first NUL. */
    size_t start = 0;
    for (int i = 0; i <= size; i++) if (i == size || dest[i] == 0) {
        need(g_utf8_validate(dest + start, i - start, NULL)); start = i + 1;
    }
    *length = size; return dest;
}
static void crack_current(MdbTableDef *table, MdbField *indexed) {
    MdbHandle *mdb = table->entry->mdb;
    MdbField raw[MAX_COLS] = {0}; int start = 0; size_t len = 0;
    need(table->cur_row > 0 && mdb_find_row(mdb, table->cur_row - 1, &start, &len) == 0);
    start &= 0x1fff;
    need(start >= 0 && start < mdb->fmt->pg_size && len > 0 && len <= (size_t)(mdb->fmt->pg_size - start));
    /* Preflight BEFORE libmdb touches the null mask or allocates variable
     * offsets. mdb_fetch_row would crack first, too early for these guards. */
    need(len >= 3);
    unsigned row_cols = mdb_get_int16(mdb->pg_buf, start);
    need(row_cols >= table->num_cols && row_cols <= MAX_COLS);
    unsigned mask = (row_cols + 7) / 8, variable = 0;
    need(len >= 2 + mask);
    size_t value_end = len - mask;
    if (table->num_var_cols) {
        need(len >= 2 + mask + 4);
        variable = mdb_get_int16(mdb->pg_buf, start + len - mask - 2);
        need(variable <= table->num_var_cols && variable <= row_cols);
        size_t directory = mask + 2 + 2 * (variable + 1);
        need(len >= 2 + directory); value_end = len - directory;
        unsigned previous = 0;
        for (unsigned i = 0; i <= variable; i++) {
            unsigned offset = mdb_get_int16(mdb->pg_buf, start + len - mask - 4 - i * 2);
            need(offset >= 2 && offset <= value_end && offset >= previous); previous = offset;
        }
    }
    for (unsigned i = 0; i < table->num_cols; i++) {
        MdbColumn *c = g_ptr_array_index(table->columns, i);
        need(c->col_num >= 0 && c->col_num < (int)row_cols);
        if (c->is_fixed) need(c->fixed_offset >= 0 && c->fixed_offset + 2 + c->col_size <= (int)value_end);
    }
    int count = mdb_crack_row(table, start, len, raw);
    need(count >= 0 && count <= MAX_COLS);
    unsigned char seen[MAX_COLS] = {0};
    for (unsigned i = 0; i < table->num_cols; i++) {
        need(raw[i].colnum >= 0 && raw[i].colnum < (int)table->num_cols && !seen[raw[i].colnum]);
        seen[raw[i].colnum] = 1;
        need(raw[i].is_null <= 1 && raw[i].siz >= 0 && raw[i].start >= 0 && raw[i].start <= mdb->fmt->pg_size);
        need(raw[i].siz <= mdb->fmt->pg_size - raw[i].start);
        indexed[raw[i].colnum] = raw[i];
    }
}
static int next_row(MdbTableDef *table, MdbField *fields) {
    MdbHandle *mdb = table->entry->mdb;
    while (1) {
        unsigned rows = 0;
        if (table->cur_pg_num) {
            need(mdb->pg_buf[0] == MDB_PAGE_DATA);
            rows = mdb_get_int16(mdb->pg_buf, mdb->fmt->row_count_offset);
            need(rows <= (unsigned)(mdb->fmt->pg_size - mdb->fmt->row_count_offset - 2) / 2);
        }
        if (!table->cur_pg_num || table->cur_row >= rows) {
            need(++table->cur_pg_num <= DB_INPUT_LIMIT / 4096 + 1);
            if (!mdb_read_next_dpg(table)) return 0;
            table->cur_row = 0; continue;
        }
        int start; size_t len;
        need(mdb_find_row(mdb, table->cur_row++, &start, &len) == 0);
        if (start & 0x4000) continue;  /* Deleted rows are never exposed. */
        need(!(start & 0x8000));       /* Relocated-row indirection not enabled. */
        crack_current(table, fields); return 1;
    }
}
typedef struct { unsigned position, hops, pages[64]; } MetadataCursor;
static void metadata_read(MdbHandle *mdb, MetadataCursor *cursor, unsigned char *out, unsigned count) {
    unsigned size = mdb->fmt->pg_size;
    while (count) {
        if (cursor->position >= size) {
            unsigned next = mdb_get_int32(mdb->pg_buf, 4);
            need(next >= 3 && next < DB_INPUT_LIMIT / 4096 && cursor->hops < 64);
            for (unsigned i = 0; i < cursor->hops; i++) need(cursor->pages[i] != next);
            cursor->pages[cursor->hops++] = next;
            need(mdb_read_pg(mdb, next) == size); cursor->position -= size - 8; continue;
        }
        unsigned piece = MIN(count, size - cursor->position);
        memcpy(out, mdb->pg_buf + cursor->position, piece);
        out += piece; count -= piece; cursor->position += piece;
    }
}
static MdbTableDef *open_table(MdbCatalogEntry *entry) {
    MdbTableDef *table = mdb_read_table(entry);
    need(table && table->num_cols >= 1 && table->num_cols <= MAX_COLS);
    need(table->num_real_idxs <= 128 && table->num_idxs <= 128 && table->num_var_cols <= MAX_COLS);
    /* Public libmdb column-name traversal, before its C-string metadata API
     * can hide an embedded NUL or truncate an overlong name. Only the bounded
     * UTF-16 names are copied; definitions/defaults/properties aren't executed. */
    MdbHandle *mdb = entry->mdb;
    MetadataCursor cursor = {0};
    cursor.position = mdb->fmt->tab_cols_start_offset + table->num_real_idxs * mdb->fmt->tab_ridx_entry_size + table->num_cols * mdb->fmt->tab_col_entry_size;
    need(mdb_read_pg(mdb, entry->table_pg) == mdb->fmt->pg_size);
    for (unsigned i = 0; i < table->num_cols; i++) {
        unsigned char length_bytes[2]; metadata_read(mdb, &cursor, length_bytes, 2);
        unsigned length = length_bytes[0] | (length_bytes[1] << 8);
        need(length >= 2 && length <= 256 && length % 2 == 0);
        char raw_name[256], converted[1024];
        metadata_read(mdb, &cursor, (unsigned char *)raw_name, length);
        /* Names aren't compressed; literal zero UTF-16 units are invalid. */
        for (unsigned j = 0; j < length; j += 2) {
            unsigned code = (unsigned char)raw_name[j] | ((unsigned char)raw_name[j + 1] << 8);
            need(code != 0 && (code < 0xd800 || code > 0xdfff));
        }
        int converted_size = mdb_unicode2ascii(mdb, raw_name, length, converted, sizeof converted);
        need(converted_size >= 1 && converted_size <= 128 && memchr(converted, 0, converted_size) == NULL);
        need(g_utf8_validate(converted, converted_size, NULL));
    }
    need(mdb_read_pg(mdb, entry->table_pg) == mdb->fmt->pg_size);
    need(mdb_read_columns(table) != NULL && table->columns->len == table->num_cols);
    table->strategy = MDB_TABLE_SCAN; table->noskip_del = 0;
    for (unsigned i = 0; i < table->num_cols; i++) {
        MdbColumn *c = g_ptr_array_index(table->columns, i);
        need(c && c->bind_ptr == NULL && c->len_ptr == NULL && c->col_size >= 0 && c->col_size <= 4096);
        need(c->col_prec >= 0 && c->col_prec <= 38 && c->col_scale >= 0 && c->col_scale <= 38);
    }
    mdb_rewind_table(table); return table;
}

static unsigned catalog(MdbHandle *mdb, MdbCatalogEntry *entries) {
    MdbCatalogEntry system = {0}; system.mdb = mdb; system.object_type = MDB_TABLE; system.table_pg = 2;
    strcpy(system.object_name, "MSysObjects");
    MdbTableDef *table = open_table(&system);
    int id = -1, label = -1, typ = -1, flags = -1;
    for (unsigned i = 0; i < table->num_cols; i++) {
        MdbColumn *c = g_ptr_array_index(table->columns, i);
        if (!strcmp(c->name, "Id")) id = i;
        if (!strcmp(c->name, "Name")) label = i;
        if (!strcmp(c->name, "Type")) typ = i;
        if (!strcmp(c->name, "Flags")) flags = i;
    }
    need(id >= 0 && label >= 0 && typ >= 0 && flags >= 0);
    unsigned n = 0, scanned = 0;
    MdbField row[MAX_COLS] = {0};
    while (next_row(table, row)) {
        need(++scanned <= 128);
        need(!row[id].is_null && row[id].siz == 4 && !row[typ].is_null && row[typ].siz == 2);
        need(!row[flags].is_null && row[flags].siz == 4);
        int type = mdb_get_int16(mdb->pg_buf, row[typ].start);
        uint32_t bits = (uint32_t)mdb_get_int32(mdb->pg_buf, row[flags].start);
        /* Forms, queries, macros, system and linked objects are never opened. */
        if (type != MDB_TABLE || (bits & 0x80000002)) continue;
        need(n < MAX_TABLES && !row[label].is_null);
        size_t len; char *value = text_value(mdb, &row[label], &len);
        need(len >= 1 && len <= 128 && memchr(value, 0, len) == NULL);
        entries[n].mdb = mdb; entries[n].object_type = MDB_TABLE;
        entries[n].table_pg = (uint32_t)mdb_get_int32(mdb->pg_buf, row[id].start) & 0x00ffffff;
        entries[n].flags = bits; memcpy(entries[n].object_name, value, len + 1); g_free(value);
        need(entries[n].table_pg >= 3 && entries[n].table_pg < DB_INPUT_LIMIT / 4096);
        n++;
    }
    need(scanned == table->num_rows); /* Reject silent short reads / stale copies. */
    mdb_free_tabledef(table); need(n > 0); return n;
}
static int previewable(int type) {
    return type == MDB_BOOL || type == MDB_BYTE || type == MDB_INT || type == MDB_LONGINT ||
        type == MDB_MONEY || type == MDB_FLOAT || type == MDB_DOUBLE || type == MDB_DATETIME ||
        type == MDB_BINARY || type == MDB_TEXT || type == MDB_REPID || type == MDB_NUMERIC;
}
static void emit_catalog(MdbCatalogEntry *entries, unsigned n) {
    emit("{\"version\":\"1.0.1\",\"tables\":[");
    for (unsigned i = 0; i < n; i++) {
        MdbTableDef *table = open_table(&entries[i]);
        if (i) emit(",");
        emit("{\"index\":"); number(i); emit(",\"label\":"); string(entries[i].object_name);
        emit(",\"columns\":[");
        for (unsigned j = 0; j < table->num_cols; j++) {
            MdbColumn *c = g_ptr_array_index(table->columns, j);
            if (j) emit(",");
            emit("{\"label\":"); string(c->name); emit(",\"type\":"); number(c->col_type);
            emit(",\"previewable\":"); emit(previewable(c->col_type) ? "true" : "false"); emit("}");
        }
        emit("]}"); mdb_free_tabledef(table);
    }
    emit("]}");
}
static void emit_cell(MdbHandle *mdb, MdbColumn *col, MdbField *field) {
    int type = col->col_type; need(previewable(type));
    if (type == MDB_BOOL) { emit(field->is_null ? "{\"type\":\"boolean\",\"value\":false}" : "{\"type\":\"boolean\",\"value\":true}"); return; }
    if (field->is_null) { emit("{\"type\":\"null\",\"value\":null}"); return; }
    if (type == MDB_BINARY) { emit("{\"type\":\"blob\",\"bytes\":"); number(field->siz); emit("}"); return; }
    if (type == MDB_TEXT) {
        size_t size; char *text = text_value(mdb, field, &size);
        if (size > 512) { emit("{\"type\":\"text-omitted\",\"bytes\":"); number(size); emit(",\"reason\":\"cell-budget\"}"); }
        else { emit("{\"type\":\"text\",\"value\":"); quoted(text, size); emit("}"); }
        g_free(text); return;
    }
    int expected = type == MDB_BYTE ? 1 : type == MDB_INT ? 2 : type == MDB_LONGINT || type == MDB_FLOAT ? 4 : type == MDB_REPID ? 16 : type == MDB_NUMERIC ? 17 : 8;
    need(field->siz == expected);
    if (type == MDB_FLOAT || type == MDB_DOUBLE || type == MDB_DATETIME) {
        double n = type == MDB_FLOAT ? mdb_get_single(mdb->pg_buf, field->start) : mdb_get_double(mdb->pg_buf, field->start);
        if (!isfinite(n)) { need(type != MDB_DATETIME); emit("{\"type\":\"nonfinite\",\"value\":null}"); return; }
        char digits[40]; snprintf(digits, sizeof digits, "%.17g", n);
        emit(type == MDB_DATETIME ? "{\"type\":\"access-date\",\"value\":" : "{\"type\":\"real\",\"value\":"); emit(digits); emit("}"); return;
    }
    char *text = type == MDB_NUMERIC ? mdb_numeric_to_string(mdb, field->start, col->col_scale, col->col_prec) :
        mdb_col_to_string(mdb, mdb->pg_buf, field->start, type, field->siz);
    need(text && strlen(text) <= 64);
    emit("{\"type\":"); string(type == MDB_MONEY || type == MDB_NUMERIC ? "decimal" : type == MDB_REPID ? "uuid" : "integer");
    emit(",\"value\":"); string(text); emit("}"); g_free(text);
}
static void emit_page(MdbCatalogEntry *entry, unsigned *columns, unsigned nc, unsigned offset, unsigned limit) {
    MdbTableDef *table = open_table(entry); MdbHandle *mdb = entry->mdb;
    for (unsigned i = 0; i < nc; i++) { need(columns[i] < table->num_cols); need(previewable(((MdbColumn *)g_ptr_array_index(table->columns, columns[i]))->col_type)); }
    unsigned ordinal = 0, returned = 0; int more = 0, eof = 0;
    emit("{\"rows\":[");
    while (1) {
        MdbField row[MAX_COLS] = {0};
        if (!next_row(table, row)) { eof = 1; break; }
        need(ordinal < 100201);
        if (ordinal++ < offset) continue;
        if (returned == limit) { more = 1; break; }
        if (returned++) emit(",");
        emit("[");
        for (unsigned i = 0; i < nc; i++) {
            if (i) emit(",");
            emit_cell(mdb, g_ptr_array_index(table->columns, columns[i]), &row[columns[i]]);
        }
        emit("]");
    }
    if (eof) need(ordinal == table->num_rows);
    emit("],\"has_more\":"); emit(more ? "true" : "false"); emit("}");
    mdb_free_tabledef(table);
}
int main(int argc, char **argv) {
    need(argc == 3 || argc == 7);
    need(!strcmp(mdb_get_version(), "1.0.1"));
    struct rlimit memory = {256 * 1024 * 1024, 256 * 1024 * 1024}, cpu = {12, 12}, core = {0, 0};
    need(!setrlimit(RLIMIT_AS, &memory) && !setrlimit(RLIMIT_CPU, &cpu) && !setrlimit(RLIMIT_CORE, &core));
    alarm(15); setlocale(LC_ALL, "C");
    struct stat info; need(!lstat(argv[1], &info) && S_ISREG(info.st_mode) && info.st_size >= 4096 && info.st_size <= DB_INPUT_LIMIT);
    need((info.st_mode & 0222) == 0);
    MdbHandle *mdb = mdb_open(argv[1], MDB_NOFLAGS); need(mdb != NULL && !mdb->f->writable && mdb->f->db_key == 0);
    need(mdb->f->jet_version == MDB_VER_JET4 || mdb->f->jet_version == MDB_VER_ACCDB_2007 || mdb->f->jet_version == MDB_VER_ACCDB_2010);
    mdb_set_bind_size(mdb, 16384); mdb_set_repid_fmt(mdb, MDB_NOBRACES_4_2_2_2_6);
    MdbCatalogEntry entries[MAX_TABLES] = {0}; unsigned n = catalog(mdb, entries);
    output = g_string_sized_new(4096);
    if (argc == 3) { need(!strcmp(argv[2], "catalog")); emit_catalog(entries, n); }
    else {
        need(!strcmp(argv[2], "page")); unsigned index = parse_uint(argv[3], 31); need(index < n);
        unsigned columns[16], nc = 0; char *copy = g_strdup(argv[4]), *token, *cursor = copy;
        while ((token = strsep(&cursor, ",")) != NULL) { need(nc < 16); columns[nc] = parse_uint(token, 127); for (unsigned j = 0; j < nc; j++) need(columns[j] != columns[nc]); nc++; }
        need(nc > 0); g_free(copy);
        unsigned offset = parse_uint(argv[5], 100000), limit = parse_uint(argv[6], 200); need(limit > 0);
        emit_page(&entries[index], columns, nc, offset, limit);
    }
    mdb_close(mdb); need(output->len <= MAX_OUTPUT);
    need(fwrite(output->str, 1, output->len, stdout) == output->len); g_string_free(output, TRUE); return 0;
}
