import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {AD_ENDCARD_FRAME, TITLE_FADE_FRAMES, type EditScene, type Resolution, type ScriptedAd} from '../src/contracts.ts';
import {graphicLabel, sceneLength} from '../src/timeline.ts';

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
  const endCard = props.ad && frame >= AD_ENDCARD_FRAME;
  const caption = props.ad?.captions.find(cue => cue.start_frame <= frame && frame < cue.end_frame);
  const label = !endCard && props.scenes && graphicLabel(props.scenes, frame);
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
    {label && <div style={{position: 'absolute', left: 60, maxWidth: 900, width: 'max-content', boxSizing: 'border-box',
      ...(label.position === 'top' ? {top: 200} : {bottom: props.ad ? 520 : 480}),
      backgroundColor: '#141714', borderLeft: `8px solid ${props.accent}`, padding: '24px 28px', borderRadius: 12,
      fontSize: label.size === 'large' ? 84 : 58, lineHeight: 1.15, fontWeight: 700, overflowWrap: 'anywhere'}}>{label.text}</div>}
    {caption && <div style={{position:'absolute',left:82,right:155,bottom:330,textAlign:'center'}}>
      <span style={{display:'inline-block',maxWidth:'100%',boxSizing:'border-box',
        background:'rgba(17,28,21,.92)',borderRadius:20,padding:'10px 24px 12px',
        fontSize:51,fontWeight:700,lineHeight:1.13,textWrap:'balance',overflowWrap:'anywhere'}}>{caption.text}</span>
    </div>}
    {endCard && <div style={{position:'absolute',left:84,right:165,bottom:320,
      background:'#f4f3eb',color:'#193325',padding:'25px 34px 29px',borderRadius:24,overflowWrap:'anywhere'}}>
      <div style={{fontSize:58,fontWeight:750,lineHeight:1.04,marginBottom:15}}>{props.ad!.brand}</div>
      <div style={{display:'inline-block',fontSize:44,fontWeight:700,lineHeight:1.12,background:'#285640',
        color:'#fff',padding:'13px 22px',borderRadius:14}}>{props.ad!.cta}</div>
    </div>}
    {props.watermark ? <div style={{position: 'absolute', ...(props.ad ? {top: 100} : {bottom: 340}), left: 60, padding: '14px 22px', backgroundColor: 'rgba(0,0,0,.65)', color: '#fff', border: '2px solid #fff', fontSize: 28}}>Creado con Agencia AI · Free</div> : null}
    </AbsoluteFill>
  </AbsoluteFill>;
};
