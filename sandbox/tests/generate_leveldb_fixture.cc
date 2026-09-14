// Original LevelDB 1.23 table writer fixture; never opens a user database.
#include <leveldb/env.h>
#include <leveldb/options.h>
#include <leveldb/table_builder.h>
#include <cstdlib>
#include <string>
void ok(leveldb::Status s) { if(!s.ok()) std::abort(); }
std::string key(std::string text,unsigned type,unsigned seq) {
  uint64_t tag=(uint64_t(seq)<<8)|type;
  for(int i=0;i<8;i++) text.push_back(char(tag>>(8*i)));
  return text;
}
int main(int argc,char**argv) {
  if(argc!=2)return 1;
  leveldb::Options o; o.compression=leveldb::kNoCompression;
  leveldb::WritableFile* f=nullptr; ok(o.env->NewWritableFile(argv[1],&f));
  leveldb::TableBuilder b(o,f);
  b.Add(key("a",1,1),"original"); b.Add(key("b",0,2),""); b.Add(key("c",1,3),leveldb::Slice("a\0b",3));
  ok(b.Finish()); ok(f->Close()); delete f;
}
