#!/usr/bin/env python3
from pathlib import Path
import cv2,numpy as np,json
ROOT=Path(__file__).resolve().parents[2]; P=ROOT/'projects/low-poly-cat'; R=P/'refs'
report={}
for view in ('front','threequarter','side'):
 img=cv2.imread(str(R/f'ref_{view}.jpg')); h,w=img.shape[:2]
 mask=np.full((h,w),cv2.GC_PR_BGD,np.uint8)
 # hard background: border/corners; probable foreground: central body envelope
 b=max(2,round(min(h,w)*.025)); mask[:b,:]=0;mask[-1:,:]=0;mask[:,:b]=0;mask[:,-b:]=0
 # foreground seeds from color distance to median border background
 border=np.concatenate([img[:b].reshape(-1,3),img[:,-b:].reshape(-1,3)],axis=0); bg=np.median(border,axis=0)
 dist=np.linalg.norm(img.astype(float)-bg,axis=2)
 # exclude floor/shadow lower corners; seed strong color/intensity differences inside central region
 yy,xx=np.mgrid[:h,:w]; central=(xx>w*.08)&(xx<w*.92)&(yy>h*.02)&(yy<h*.98)
 mask[(dist>28)&central]=cv2.GC_PR_FGD
 mask[(dist>52)&central]=cv2.GC_FGD
 bgd=np.zeros((1,65),np.float64);fgd=np.zeros((1,65),np.float64)
 cv2.grabCut(img,mask,None,bgd,fgd,8,cv2.GC_INIT_WITH_MASK)
 out=np.where((mask==1)|(mask==3),255,0).astype('uint8')
 # retain largest foreground component; fill internal holes
 n,lab,stats,_=cv2.connectedComponentsWithStats(out,8); keep=1+np.argmax(stats[1:,cv2.CC_STAT_AREA]); out=np.where(lab==keep,255,0).astype('uint8')
 cnt,_=cv2.findContours(out,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE); cv2.drawContours(out,cnt,-1,255,cv2.FILLED)
 cv2.imwrite(str(R/f'mask_{view}_v2.png'),out)
 x,y,bw,bh=cv2.boundingRect(cnt[0] if len(cnt)==1 else max(cnt,key=cv2.contourArea)); report[view]={'size':[w,h],'bbox':[x,y,x+bw,y+bh],'area':int((out>0).sum())}
(R/'segmentation_v2.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))