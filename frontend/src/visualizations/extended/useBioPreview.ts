import {onScopeDispose,ref,shallowRef,watch} from 'vue';
import type {FileInfo} from '../../api/file';
import type {VisualizationPlugin} from '../contract';
import {usePreviewLoad} from '../../composables/usePreviewLoad';
import {filePreviewIdentity,pluginPreviewIdentity} from '../previewIdentity';
import {requestVisualization} from '../runtime';
import {displayError} from './domains/lifecycle';
import {bioCatalogIdentity,bioSelection,parseBioData,type BioData,type BioKind,type BioReader} from './sequenceBrowserData';
export function useBioPreview(file:()=>FileInfo,plugin:()=>VisualizationPlugin,reader:BioReader) {
  const scope=usePreviewLoad(),catalog=shallowRef<BioData>(),displayed=shallowRef<BioData>(),busy=ref(false),error=ref('');let version:string|undefined;
  const clear=()=>{scope.begin();displayed.value=undefined;busy.value=false;error.value='';};
  async function inspect(){const load=scope.begin();catalog.value=undefined;displayed.value=undefined;version=undefined;busy.value=false;error.value='';if(!plugin().enabled)return;busy.value=true;
    try{const r=await requestVisualization(file(),plugin(),'preview',{kind:'tree'},load.signal);load.assertCurrent();catalog.value=parseBioData(r,reader,'tree',{},file().size,file().filename,plugin().id);version=r.version;}
    catch(e){if(load.isCurrent())error.value=displayError(e);}finally{if(load.isCurrent())busy.value=false;}}
  async function loadData(options:Record<string,unknown>){const load=scope.begin();displayed.value=undefined;busy.value=true;error.value='';
    try{if(!catalog.value||!version||!plugin().enabled)throw new Error('请先检查已启用插件的文件目录。');const expected=catalog.value,pinned=version,kind:BioKind=reader==='genome-tracks'?'map':'table';const selected=bioSelection(reader,kind,options);
      const r=await requestVisualization(file(),plugin(),'preview',{kind,version:pinned,...selected},load.signal);load.assertCurrent();if(r.version!==pinned)throw new Error('文件版本已变化，请重新打开预览。');const parsed=parseBioData(r,reader,kind,selected,file().size,file().filename,plugin().id);if(bioCatalogIdentity(parsed)!==bioCatalogIdentity(expected))throw new Error('目录与所选数据不一致，请重新打开。');displayed.value=parsed;
    }catch(e){if(load.isCurrent())error.value=displayError(e);}finally{if(load.isCurrent())busy.value=false;}}
  watch([()=>filePreviewIdentity(file()),()=>pluginPreviewIdentity(plugin())],()=>void inspect(),{immediate:true,flush:'sync'});
  onScopeDispose(()=>{catalog.value=undefined;displayed.value=undefined;version=undefined;});
  return {catalog,displayed,busy,error,clear,inspect,loadData};
}
