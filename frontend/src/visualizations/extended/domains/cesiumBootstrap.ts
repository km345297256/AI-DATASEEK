export type CesiumBasemap = 'natural-earth' | 'grid' | 'none';

export function isCesiumBasemap(value: unknown): value is CesiumBasemap {
  return value === 'natural-earth' || value === 'grid' || value === 'none';
}

/** Source-controlled frame code only: dataset values arrive by structured clone. */
export const cesiumBootstrap = `let viewer,loaded=false,disposed=false,baseLayer,removeBaseError,baseRequest=0,baseTimeout;
const active=(request)=>!disposed&&viewer&&!viewer.isDestroyed()&&request===baseRequest;
function clearBaseTimeout(){if(baseTimeout!==undefined){clearTimeout(baseTimeout);baseTimeout=undefined;}}
function clearBasemap(){
 if(removeBaseError){removeBaseError();removeBaseError=undefined;}
 if(baseLayer){viewer.imageryLayers.remove(baseLayer,true);baseLayer=undefined;}
}
async function setBasemap(mode,fallback=false){
 if(!['natural-earth','grid','none'].includes(mode)||disposed||!viewer||viewer.isDestroyed())return;
 clearBaseTimeout();
 const request=++baseRequest,C=window.Cesium;
 notify('basemap',{mode,status:'loading',fallback});
 try{
  let provider;
  if(mode==='natural-earth'){
   const pending=C.TileMapServiceImageryProvider.fromUrl(assetRoot+'cesium/Assets/Textures/NaturalEarthII/',{
    fileExtension:'jpg',minimumLevel:0,maximumLevel:2,
    tilingScheme:new C.GeographicTilingScheme({ellipsoid:C.Ellipsoid.WGS84}),
    credit:new C.Credit('Natural Earth II (public domain)',true)
   });
   provider=await Promise.race([pending,new Promise((_,reject)=>{baseTimeout=setTimeout(()=>reject(new Error('local_basemap_timeout')),10000);})]);
  }
  else if(mode==='grid')provider=new C.GridImageryProvider({
   tilingScheme:new C.GeographicTilingScheme({ellipsoid:C.Ellipsoid.WGS84}),cells:8,
   color:C.Color.fromCssColorString('#65839a'),glowColor:C.Color.TRANSPARENT,
   backgroundColor:C.Color.fromCssColorString('#203345')
  });
  if(!active(request))return;
  clearBaseTimeout();
  const nextLayer=provider?viewer.imageryLayers.addImageryProvider(provider,0):undefined;
  clearBasemap();baseLayer=nextLayer;
  if(mode==='natural-earth')removeBaseError=provider.errorEvent.addEventListener(()=>{
   if(active(request))void setBasemap('grid',true);
  });
  viewer.scene.requestRender();notify('basemap',{mode,status:'ready',fallback});
 }catch{
  if(!active(request))return;
  clearBaseTimeout();
  if(mode==='natural-earth'){await setBasemap('grid',true);return;}
  clearBasemap();viewer.scene.requestRender();notify('basemap',{mode:'none',status:'ready',fallback:true});
 }
}
window.addEventListener('pagehide',()=>{
 disposed=true;baseRequest++;clearBaseTimeout();
 if(viewer&&!viewer.isDestroyed()){clearBasemap();viewer.destroy();}
});
window.addEventListener('message',async(event)=>{
 if(!trusted(event))return;
 if(event.data.type==='basemap'){await setBasemap(event.data.payload?.mode);return;}
 if(event.data.type!=='load'||loaded||disposed)return;loaded=true;
 try{
  const {data,czml}=event.data.payload,C=window.Cesium;
  viewer=new C.Viewer(document.getElementById('viewer'),{baseLayer:false,baseLayerPicker:false,geocoder:false,homeButton:true,sceneModePicker:true,navigationHelpButton:false,animation:czml,timeline:czml,fullscreenButton:false,infoBox:false,selectionIndicator:false,skyBox:false,skyAtmosphere:false,terrainProvider:new C.EllipsoidTerrainProvider({ellipsoid:C.Ellipsoid.WGS84}),requestRenderMode:false});
  viewer.scene.globe.baseColor=C.Color.fromCssColorString('#203345');viewer.scene.globe.enableLighting=false;
  void setBasemap('natural-earth');
  const source=czml?await C.CzmlDataSource.load(data):await C.GeoJsonDataSource.load(data,{clampToGround:false,markerColor:C.Color.ORANGE,stroke:C.Color.CYAN,fill:C.Color.CYAN.withAlpha(0.25)});
  if(disposed||viewer.isDestroyed())return;
  await viewer.dataSources.add(source);
  if(disposed||viewer.isDestroyed())return;
  await viewer.zoomTo(source);
  if(disposed||viewer.isDestroyed())return;
  viewer.scene.requestRenderMode=true;viewer.scene.requestRender();notify('ready');
 }catch{if(!disposed)notify('error');}
});notify('boot');`;
