import React from 'react';
import {AbsoluteFill, OffthreadVideo, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {TITLE_FADE_FRAMES, type Resolution} from '../src/contracts.ts';

export interface ReelProps extends Record<string, unknown> {title: string; subtitle: string; subtitle_size?: 'large'; duration_seconds: number; background: string; accent: string; watermark: boolean; video_file: string | null; resolution: Resolution; transparent: boolean; overlay_only?: boolean}
export const Reel: React.FC<ReelProps> = (props) => {
  const frame = useCurrentFrame();
  const scale = useVideoConfig().width / 1080;
  const opacity = interpolate(frame, [0, TITLE_FADE_FRAMES], [0, 1], {extrapolateRight: 'clamp'});
  // Todo movimiento depende del fotograma; no se utiliza reloj ni azar.
  return <AbsoluteFill style={{backgroundColor: props.transparent ? undefined : props.background, color: '#fff', fontFamily: 'Arial, sans-serif'}}>
    {props.video_file ? <OffthreadVideo src={staticFile(props.video_file)} transparent={props.transparent} muted style={{width: '100%', height: '100%', objectFit: 'cover'}} /> : null}
    {props.transparent && !props.overlay_only ? null : <AbsoluteFill style={{background: 'linear-gradient(180deg,rgba(0,0,0,.45),transparent 55%,rgba(0,0,0,.8))'}} />}
    <AbsoluteFill style={{width: 1080, height: 1920, transform: `scale(${scale})`, transformOrigin: 'top left'}}>
    <div style={{position: 'absolute', left: 60, right: 120, top: 200, opacity, backgroundColor: '#141714', padding: '32px 38px', borderRadius: 20}}>
      <div style={{height: 8, width: 120, backgroundColor: props.accent, marginBottom: 32}} />
      <h1 style={{fontSize: 76, lineHeight: 1.1, margin: 0, overflowWrap: 'anywhere'}}>{props.title}</h1>
      {props.subtitle && <p style={{fontSize: props.subtitle_size === 'large' ? 64 : 50, lineHeight: 1.25, margin: '20px 0 0', overflowWrap: 'anywhere'}}>{props.subtitle}</p>}
    </div>
    {props.watermark ? <div style={{position: 'absolute', bottom: 340, left: 60, padding: '14px 22px', backgroundColor: 'rgba(0,0,0,.65)', color: '#fff', border: '2px solid #fff', fontSize: 28}}>Creado con Agencia AI · Free</div> : null}
    </AbsoluteFill>
  </AbsoluteFill>;
};
