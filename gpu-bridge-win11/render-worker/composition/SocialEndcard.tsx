import React from 'react';
import {Easing, interpolate} from 'remotion';
import {SOCIAL_ENDCARD_FRAME, SOCIAL_ENTRANCE_FRAMES, type AdDestination} from '../src/contracts.ts';
import {socialFadeProgress} from '../src/timeline.ts';

import {socialIcons} from '../src/social-icons.ts';

const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;
export function SocialEndcard({destination, brand, cta, frame}: {
  destination: AdDestination; brand: string; cta: string; frame: number;
}) {
  const reveal = interpolate(frame, [SOCIAL_ENDCARD_FRAME, SOCIAL_ENDCARD_FRAME + SOCIAL_ENTRANCE_FRAMES], [0, 1],
    {...clamp, easing: Easing.out(Easing.cubic)});
  const fade = 1 - socialFadeProgress(frame);
  const icon = socialIcons[destination.channel];
  const address = destination.url.replace(/^https:\/\/(?:www\.)?/u, '');
  return <div style={{position:'absolute',left:0,right:0,bottom:0,height:610,opacity:reveal*fade,
    background:'linear-gradient(0deg, rgba(11,21,17,.98) 30%, rgba(20,37,28,.86) 63%, transparent)'}}>
    <div style={{position:'absolute',left:85,right:150,bottom:205,transform:`translateY(${(1-reveal)*45}px)`}}>
      <div style={{fontSize:54,fontWeight:750,lineHeight:1.05,textShadow:'0 2px 8px #12241b',marginBottom:14,overflowWrap:'anywhere'}}>{brand}</div>
      <div style={{display:'flex',alignItems:'center',gap:20}}>
        <svg role="img" aria-label={icon.name} viewBox="0 0 24 24" width={59} height={59}
          style={{flexShrink:0,background:'#fff',borderRadius:14,padding:8,boxSizing:'content-box'}}>
          <path fill={icon.color} d={icon.path} />
        </svg>
        <div style={{minWidth:0}}>
          <div style={{fontSize:44,fontWeight:750,lineHeight:1.05,overflowWrap:'anywhere'}}>{cta}</div>
          <div style={{fontSize:address.length > 43 ? 32 : 40,lineHeight:1.12,overflowWrap:'anywhere',color:'#e7f2d2'}}>{address}</div>
        </div>
      </div>
    </div>
  </div>;
}
