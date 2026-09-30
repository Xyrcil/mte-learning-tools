import importlib.util,sys,tempfile,unittest,zipfile
from pathlib import Path
p=Path(__file__).with_name('prepare_runtime_base.py')
s=importlib.util.spec_from_file_location('runtime_base',p);module=importlib.util.module_from_spec(s);sys.modules[s.name]=module;s.loader.exec_module(module)

class RuntimeBaseTests(unittest.TestCase):
    def game(self,abi):return {f'lib/{abi}/{name}' for name in module.GAME_LIBRARIES}
    def runtime(self,abi):return {f'lib/{abi}/libchaquopy_java.so',f'lib/{abi}/libpython3.11.so'}
    def test_only_complete_intersection_is_advertised(self):
        names=self.game('arm64-v8a')|self.game('armeabi-v7a')|{'lib/x86/libsome_sdk.so'}
        self.assertEqual(module.compatible_abis(names,self.runtime('arm64-v8a')),{'arm64-v8a'})
        with self.assertRaises(ValueError):module.compatible_abis(self.game('armeabi-v7a'),self.runtime('arm64-v8a'))
    def test_preparation_preserves_inputs_and_every_retained_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'base.apk';helper=root/'helper.apk';output=root/'filtered.apk'
            names=self.game('arm64-v8a')|self.game('armeabi-v7a')|{'classes.dex','AndroidManifest.xml','assets/content.json'}
            with zipfile.ZipFile(base,'w') as z:
                for name in names:z.writestr(name,('original:'+name).encode())
            with zipfile.ZipFile(helper,'w') as z:
                for name in self.runtime('arm64-v8a'):z.writestr(name,b'helper')
            old=base.read_bytes();runtime=helper.read_bytes();report=module.prepare(base,helper,output)
            self.assertEqual(base.read_bytes(),old);self.assertEqual(helper.read_bytes(),runtime)
            self.assertEqual(set(report['removed_incomplete_abi_entries']),self.game('armeabi-v7a'))
            with zipfile.ZipFile(base) as before,zipfile.ZipFile(output) as after:
                for name in after.namelist():self.assertEqual(before.read(name),after.read(name))
            with self.assertRaises(ValueError):module.prepare(base,helper,output)

if __name__=='__main__':unittest.main()
