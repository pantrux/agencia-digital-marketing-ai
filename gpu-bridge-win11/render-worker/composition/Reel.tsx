import React from 'react';
import {AbsoluteFill, OffthreadVideo, Sequence, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {TITLE_FADE_FRAMES, type EditScene, type Resolution} from '../src/contracts.ts';
import {graphicLabel, sceneLength} from '../src/timeline.ts';

export interface ReelProps extends Record<string, unknown> {title: string; subtitle: string; subtitle_size?: 'large'; duration_seconds: number; background: string; accent: string; watermark: boolean; video_file: string | null; resolution: Resolution; transparent: boolean; overlay_only?: boolean; scenes?: EditScene[]}
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
  const label = props.scenes && graphicLabel(props.scenes, frame);
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
    {label && <div style={{position: 'absolute', left: 60, right: 120, ...(label.position === 'top' ? {top: 200} : {bottom: 480}),
      backgroundColor: '#141714', borderLeft: `8px solid ${props.accent}`, padding: '24px 28px', borderRadius: 12,
      fontSize: 58, lineHeight: 1.15, fontWeight: 700, overflowWrap: 'anywhere'}}>{label.text}</div>}
    {props.watermark ? <div style={{position: 'absolute', bottom: 340, left: 60, padding: '14px 22px', backgroundColor: 'rgba(0,0,0,.65)', color: '#fff', border: '2px solid #fff', fontSize: 28}}>Creado con Agencia AI · Free</div> : null}
    </AbsoluteFill>
  </AbsoluteFill>;
};
