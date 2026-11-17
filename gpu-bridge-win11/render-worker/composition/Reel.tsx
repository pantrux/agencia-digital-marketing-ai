import React from 'react';
import {AbsoluteFill, Easing, OffthreadVideo, Sequence, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {AD_ENDCARD_FRAME, LABEL_ENTRANCE_FRAMES, SOCIAL_ENDCARD_FRAME, TITLE_FADE_FRAMES, type EditScene, type Resolution, type ScriptedAd} from '../src/contracts.ts';
import {graphicLabelTiming, sceneLength} from '../src/timeline.ts';
import {SocialEndcard} from './SocialEndcard.tsx';

export interface ReelProps extends Record<string, unknown> {title: string; subtitle: string; subtitle_size?: 'large'; duration_seconds: number; background: string; accent: string; watermark: boolean; video_file: string | null; resolution: Resolution; transparent: boolean; overlay_only?: boolean; scenes?: EditScene[]; ad?: ScriptedAd}
const videoStyle = {width: '100%', height: '100%', objectFit: 'cover'} as const;

function cutVideo(file: string, scenes: EditScene[]) {
  let from = 0;
  return scenes.map(scene => {
    const durationInFrames = sceneLength(scene);
    const start = from;
    from += durationInFrames;
    return <Sequence key={start} from={start} durationInFrames={durationInFrames}>
      <OffthreadVideo src={staticFile(file)} trimBefore={scene.source_start_frame} muted style={videoStyle} />
    </Sequence>;
  });
}

export const Reel: React.FC<ReelProps> = (props) => {
  const frame = useCurrentFrame();
  const scale = useVideoConfig().width / 1080;
  const opacity = interpolate(frame, [0, TITLE_FADE_FRAMES], [0, 1], {extrapolateRight: 'clamp'});
  const social = props.ad?.destination;
  const endCard = props.ad && frame >= (social ? SOCIAL_ENDCARD_FRAME : AD_ENDCARD_FRAME);
  const caption = props.ad?.captions.find(cue => cue.start_frame <= frame && frame < cue.end_frame);
  const activeLabel = !endCard && props.scenes ? graphicLabelTiming(props.scenes, frame) : undefined;
  const label = activeLabel?.label;
  const entrance = social && activeLabel ? interpolate(activeLabel.age, [0, LABEL_ENTRANCE_FRAMES], [0,1],
    {extrapolateRight:'clamp',easing:Easing.out(Easing.cubic)}) : 1;
  const hook = social && frame < sceneLength(props.scenes?.[0] ?? {source_start_frame:0,source_end_frame:0});
  const captionLayout = (social && endCard) || caption?.position === 'top'
    ? {left:82,right:155,top:330,textAlign:'left' as const}
    : caption?.position === 'left' ? {left:60,width:285,bottom:405,textAlign:'left' as const}
    : caption?.position === 'right' ? {right:125,width:430,bottom:570,textAlign:'left' as const}
    : {left:82,right:155,bottom:330,textAlign:'center' as const};
  // Todo movimiento depende del fotograma; no se utiliza reloj ni azar.
  return <AbsoluteFill style={{backgroundColor: props.transparent ? undefined : props.background, color: '#fff', fontFamily: 'Arial, sans-serif'}}>
    {props.video_file && (props.scenes
      ? cutVideo(props.video_file, props.scenes)
      : <OffthreadVideo src={staticFile(props.video_file)} transparent={props.transparent} muted style={videoStyle} />)}
    {!props.scenes && !(props.transparent && !props.overlay_only) && <AbsoluteFill style={{background: 'linear-gradient(180deg,rgba(0,0,0,.45),transparent 55%,rgba(0,0,0,.8))'}} />}
    <AbsoluteFill style={{width: 1080, height: 1920, transform: `scale(${scale})`, transformOrigin: 'top left'}}>
    {!props.scenes && <div style={{position: 'absolute', left: 60, right: 120, top: 200, opacity, backgroundColor: '#141714', padding: '32px 38px', borderRadius: 20}}>
      <div style={{height: 8, width: 120, backgroundColor: props.accent, marginBottom: 32}} />
      <h1 style={{fontSize: 76, lineHeight: 1.1, margin: 0, overflowWrap: 'anywhere'}}>{props.title}</h1>
      {props.subtitle && <p style={{fontSize: props.subtitle_size === 'large' ? 64 : 50, lineHeight: 1.25, margin: '20px 0 0', overflowWrap: 'anywhere'}}>{props.subtitle}</p>}
    </div>}
    {label && <div style={{position: 'absolute', ...(hook ? {right:122,maxWidth:380} : {left:60,maxWidth:900}),
      width: 'max-content', boxSizing: 'border-box',
      ...(label.position === 'top' ? {top: social ? 205 : 200} : {bottom: props.ad ? 520 : 480}),
      backgroundColor: social ? 'rgba(16,32,24,.88)' : '#141714', borderLeft: `8px solid ${props.accent}`,
      padding: social ? '16px 22px' : '24px 28px', borderRadius: social ? 23 : 12,
      opacity:entrance,transform:social ? `translateY(${(1-entrance)*-18}px)` : undefined,
      fontSize: hook ? 52 : label.size === 'large' ? 84 : 58, lineHeight: social ? 1.12 : 1.15,
      fontWeight: 700, overflowWrap: 'anywhere'}}>{label.text}</div>}
    {caption && <div style={{position:'absolute',...captionLayout}}>
      <span style={{display:'inline-block',maxWidth:'100%',boxSizing:'border-box',
        background:'rgba(17,28,21,.92)',borderRadius:20,padding:'10px 24px 12px',
        fontSize:51,fontWeight:700,lineHeight:1.13,textWrap:'balance',overflowWrap:'anywhere'}}>{caption.text}</span>
    </div>}
    {endCard && social && props.ad && <SocialEndcard destination={social} brand={props.ad.brand}
      cta={props.ad.cta} frame={frame} alpha={props.transparent} />}
    {endCard && !social && <div style={{position:'absolute',left:84,right:165,bottom:320,
      background:'#f4f3eb',color:'#193325',padding:'25px 34px 29px',borderRadius:24,overflowWrap:'anywhere'}}>
      <div style={{fontSize:58,fontWeight:750,lineHeight:1.04,marginBottom:15}}>{props.ad!.brand}</div>
      <div style={{display:'inline-block',fontSize:44,fontWeight:700,lineHeight:1.12,background:'#285640',
        color:'#fff',padding:'13px 22px',borderRadius:14}}>{props.ad!.cta}</div>
    </div>}
    {props.watermark ? <div style={{position: 'absolute', ...(props.ad ? {top: 100} : {bottom: 340}), left: 60, padding: '14px 22px', backgroundColor: 'rgba(0,0,0,.65)', color: '#fff', border: '2px solid #fff', fontSize: 28}}>Creado con Agencia AI · Free</div> : null}
    </AbsoluteFill>
  </AbsoluteFill>;
};
