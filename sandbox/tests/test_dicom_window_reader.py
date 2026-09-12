import copy
import importlib.util
import json
from pathlib import Path
import struct
import unittest
from app.services.dicom_window_reader import dicom_window_preview
from app.services.dicom_window_payload import ERROR, validate_dicom_window_options, validate_dicom_window_payload
from dicom_window_fixtures import fixture,preview,element

OPTIONS={'frame':1,'roi':[1,1,2,2],'confirm_deidentified':True}

class DicomTests(unittest.TestCase):
    def test_true_ranges_and_values(self):
        for explicit in (True,False):
            for bits in (8,16):
                for mono in ('MONOCHROME1','MONOCHROME2'):
                    with self.subTest(explicit=explicit,bits=bits,mono=mono):
                        data,values=fixture(explicit=explicit,bits=bits,mono=mono)
                        tree,calls=preview(data);start=tree['metadata']['header_bytes']
                        self.assertTrue(all(o+n<=start for o,n in calls));self.assertNotIn('array',tree)
                        self.assertNotIn('SYNTHETIC',json.dumps(tree));self.assertNotIn('1.2.826',json.dumps(tree))
                        image,reads=preview(data,'image',OPTIONS)
                        self.assertEqual(image['array']['values'],[values[i] for i in (17,18,21,22)])
                        self.assertEqual(image['metadata']['read_requests'],len(reads));self.assertEqual(image['metadata']['read_bytes'],sum(n for _,n in reads))
                        self.assertTrue(any(o>=start for o,n in reads));self.assertLess(image['metadata']['read_bytes'],len(data))
    def test_signed_bits_and_padding(self):
        values=[-2048,-1,0,1,2047,-33,24,-56,10,20,30,40]
        data,_=fixture(signed=True,stored=12,pixel_values=values,overrides={0x00280120:(b'SS',-2048)})
        image,_=preview(data,'image',{'frame':0,'roi':[0,0,4,3],'confirm_deidentified':True})
        self.assertEqual(image['array']['values'],values);self.assertEqual(image['metadata']['padding_pixels'],1)
    def test_odd_byte_padding(self):
        data,_=fixture(bits=8,frames=1,rows=1,columns=3)
        image,_=preview(data,'image',{'frame':0,'roi':[0,0,3,1],'confirm_deidentified':True})
        self.assertEqual(image['array']['values'],[0,10,20])
        with self.assertRaisesRegex(ValueError,ERROR):preview(data[:-1]+b'x','image',{'frame':0,'roi':[0,0,3,1],'confirm_deidentified':True})
    def test_options_rejected_before_read(self):
        invalid=[('tree',{'frame':0}),('tree',[]),('image',{}),('image',{**OPTIONS,'confirm_deidentified':False}),('image',{**OPTIONS,'confirm_deidentified':1}),('image',{**OPTIONS,'frame':True}),('image',{**OPTIONS,'roi':[0,0,129,1]}),('image',{**OPTIONS,'roi':[0,0,1.1,1]}),('image',{**OPTIONS,'window':100})]
        for kind,options in invalid:
            with self.subTest(options=options):
                def fail(*args):self.fail('Read happened before selection validation')
                with self.assertRaises(ValueError):dicom_window_preview(fail,1000,kind,options)
    def test_unsupported_metadata_refused_without_pixel_read(self):
        overrides=[{0x00120062:(b'CS','NO')},{0x00280301:(b'CS','YES')},{0x00280302:(b'CS','YES')},{0x00280004:(b'CS','RGB')},{0x00280002:(b'US',3)},
            {0x00280102:(b'US',14)},{0x00281053:(b'DS','0')},{0x00281050:(b'DS','1\\2')},{0x00281051:(b'DS','0')},{0x00281056:(b'CS','SIGMOID')},
            {0x00283000:(b'SQ',b'')},{0x52009230:(b'SQ',b'')},{0x60000010:(b'US',3)},{0x00281054:(b'LO','/Users/private')},{0x00281052:(b'DS','1e-999')}]
        for change in overrides:
            with self.subTest(change=change):
                data,_=fixture(overrides=change);reads=[]
                with self.assertRaisesRegex(ValueError,ERROR):dicom_window_preview(lambda o,n:reads.append((o,n)) or data[o:o+n],len(data),'image',OPTIONS)
                self.assertTrue(all(o+n<=len(data)-48 for o,n in reads))
        for tag in (0x00120062,0x00280301,0x00281052,0x00281050):
            with self.subTest(drop=tag):
                with self.assertRaises(ValueError):preview(fixture(drop=(tag,))[0])
    def test_invalid_file_encodings_and_layout(self):
        data,_=fixture()
        bad=[data[:128]+b'FAKE'+data[132:],data[:-2],data+b'\0\0',data.replace(b'1.2.840.10008.1.2.1',b'1.2.840.10008.1.2.2'),
             data.replace(b'OW\0\0\x30\0\0\0',b'OW\0\0\xff\xff\xff\xff'),data.replace(b'OW\0\0\x30\0\0\0',b'OB\0\0\x30\0\0\0')]
        for raw in bad:
            with self.subTest(length=len(raw)):
                with self.assertRaises(ValueError):preview(raw)
    def test_response_validator_strict_mutations_and_binding(self):
        raw,_=fixture();r,_=preview(raw,'image',OPTIONS)
        mutations=[lambda v:v.update(contract_version=True),lambda v:v['metadata'].update(path='/private'),lambda v:v['metadata'].update(source_bytes=True),
            lambda v:v['metadata']['limits'].update(max_roi=True),lambda v:v['array']['values'].__setitem__(0,True),lambda v:v['array'].update(shape=[True,2]),
            lambda v:v['choices']['image']['rescale'].update(unit='<img>'),lambda v:v['choices']['image']['rescale'].update(slope=0),lambda v:v['selected'].update(confirm_deidentified=1),
            lambda v:v['metadata'].update(padding_pixels=1),lambda v:v['choices']['image'].update(frames=3),lambda v:v['array'].update(dtype='uint32')]
        for mutate in mutations:
            value=copy.deepcopy(r);mutate(value)
            with self.subTest(value=value):
                with self.assertRaises(ValueError):validate_dicom_window_payload(value)
        for binding in ({'source_bytes':len(raw)+1},{'read_bytes':1},{'read_requests':1},{'fmt':'png'},{'options':{**OPTIONS,'frame':0}},{'kind':'tree'}):
            with self.assertRaises(ValueError):validate_dicom_window_payload(r,**binding)
    def test_all_pixel_ranges_budgeted_before_read(self):
        raw,_=fixture();tree,calls=preview(raw)
        budgets={'max_read_bytes':1048576,'max_total_bytes':tree['metadata']['read_bytes']+1,'max_reads':128}
        seen=[]
        with self.assertRaises(ValueError):dicom_window_preview(lambda o,n:seen.append((o,n)) or raw[o:o+n],len(raw),'image',OPTIONS,budgets)
        self.assertEqual(seen,calls)
    def test_cancellation_error_and_bad_callback_never_disclose_paths(self):
        for callback in (lambda o,n:(_ for _ in ()).throw(RuntimeError('/Users/secret/key')),lambda o,n:b'',lambda o,n:bytearray(n)):
            with self.assertRaisesRegex(ValueError,'^'+ERROR+'$'):dicom_window_preview(callback,1000)
    def test_backend_copy_identical(self):
        root=Path(__file__).resolve().parents[1]
        other=root.parent/'backend/app/application/services/dicom_window_visualization.py'
        self.assertTrue(other.exists());self.assertEqual(other.read_bytes(),(root/'app/services/dicom_window_payload.py').read_bytes())

if __name__=='__main__':unittest.main()
