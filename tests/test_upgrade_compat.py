"""First-hop upgrade regression: the frozen 0.16.0 launcher AND updater execute.

Only OS/network/SQLite boundaries are replaced. Package validation, checksum,
request copying, handoff, classification, backup/swap/rollback and unit rendering
use the real functions. The frozen sources below are from 8b2e0e38 (0.16.0),
compressed only to keep a self-contained regression independent of git history.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

from tests.test_release_update_safety import make_release, zip_tree, install_library

ROOT = Path(__file__).resolve().parents[1]
# Frozen release source, not a substitute implementation of the legacy path.
OLD_UPDATER = b"ABzY8000000t4+mX?GjPk>B|%CZnfEj0}LJF56VFd9=-}w6TsS%1*L0cRU0JBtl%70Z7pd_DN1;N!IC&?bwnMTRvjPi6!N-WF66u2?3Iy^cS{{?w+0t03CZjt^7;?Gu_qI)z#H?b@z_%jkSGmY`#(-bL-1SbG5bDsP7puP4j5etv}~Fp0RP^PaA){(7XP(-n)0Y?|=T};koq(XL=u=?Vf$3d;ix@9-jZ_sn<tFx{uFn-F(nJbFTZzrS7?}y65f~C8y<#wVUwadSgcSjZ3}rpKac}_~hZmt<#UYXD;<FebjsR?D~VRdcXN<^WGme{&0Wu>=)g;e<pYvZ-3tV>pM>#UK=q4R)$~nD$SNJLFSvQMzQMnex<x><Z{Kf=ehNk@xr0w@T*cUHfojn0{xuL<yO2(%gxo?mSY^7I&pArD5$x*Q*(WQR<2ax6Dw{TmeXuj5k1fSdE50{R0GL7QuZ1(!?w%qR@-xJ+o;r<4X<T5^?IY_v?`6dKQbbpc?(U)^IiFS(eW3nm3jG-Kj1;W-KtdO)1Uc`y8Pbo<+o<lX_XsZP5$;2P=8Uc?YHNfUZd#xs;0lHzP20>DWZN>YU+F2t0E$<=QX^@GeH6Q^tDQpfDj#;P78Y^iEs>l@{87L6Gu@zpQ^8pjLbZLaQg6+Q7|S(o|}69M=u?-e>6RP41Vt!IXFG_;)#RykEafuuxH@Ov*Ys7p(7`zkH0+iB0ZkSkB_`GGkx5i`uFK0C*Xl%_S{Q?U7?6Mvcou3_ghZ2>XwXlz2tgEYtc0-{LJtgjh3;p=+=#T1F6$;Yesw7sQ5;?0j0Arlu*rwy4Vu{29-Vc0KpK&p={k<2DrsVr@r8p^2Q8soB>aKU;rrTKnuuiK0wY_8^xsqhL7A)1aP&sUp402m1+s0Kr?^_unD|uS7F*N06mNOk(XY!U!FcbbLi+1?B{rXVj{oa#HY`_bm+wwkQ!^izlLF+d}hDBf3G=dWX$$*j(_Fa^`&}arJk!+>g|&`Y|+dbrsM3{yW82f$C-Ec?%uP1@A$KO_Koi^J^l3f-rW;>OS_AuqO)gw{@IEB&+Ogf?%O>+U)=xn)BB!v_LX*zPwd%kT3K$>alGPU(5U#C(rRg9uRCAbU*0`FKEC(q-TU`W?Au*(obt0z&p$I?+Oxm-^fS-wbM}qz+2cOzJ~Kb>JhNy2{^C3oezrJX8Xw12JJ@p+2|$Z{tI?>!TwNw|%=~ob#Pnf&hcwBUV>rBHNOPx(;t??r&x$Y~8}!T?896?E?8T|)r-O<x;j!$BJ3m&c_$~YnGpPn*0lx|ffnOJZ(N>&Q34GqI7Z+=ex0Gu-#U*FK1?Up^4-dUKP0*Qa%~gQ-Gq>1+F4TZF{8Vs={Mi7Oo(GoB`>wZKDY~)eC?N9gNvGDVs+Y&7r(QTbov)R`59BY;t-6lSvXAP^7qTz0`Y!-RE=dddBwwOOm4$l4QxD;pKSmR)4l;-9TO-GgA3ZVs{E6uo>|@8Le|YF8It^%Xk=0ik-jWYwbJY`nsZxc(l|O66W)A596H7hIFT;u#{935Af*;Hp5hRXG9iG-EOo))GBY;*6t1watIE7HsF$yDOA_I@W)X#l;{{Wir)&Plb8SKbN$t@eU-vWNOmov*wwe3!V@bWAp_iy+)Nj+=1CtE;L_{~L(hG&?jm8W_ci^^2WR1BVhbn{m#t;LMFY+6_}_WWhjV!-&%b75_*Q=!?3$+?lpGa{vw{G+}BB1ehdh6mz4%d#3pvg6}QJ|<qnl7wq}rnLwXoTs$mwPD5HVH6w93drI{8&=)2zFuTea@R|aS292{dycniH0sq=c-$~5En~@bn?8JDGyux#0bDX*!Su)QuPx@DEl}`DQCW10OFp&IT4@;0g6Fzc-uOxWr}_Uh{*6C#UNL_}{g}s@djufM2PJnoe}p<(H;x>DHKZl@0SMw$n~NahVMTGOMistQnJ)Pexu)31r(yIwHxJrY6DE1aGc)7An4QQyJNHUym-R|M{a5A(lerAE{tHCu7hVxxtC!XfK>FD;Ct!hQsR{*INaFMYK~%)QD1V8~NIS9_?<lLa+92O%EA>{^2>!s!Ipz~k#g1WZ<JDIu^1JhUUVYUVgYV1q18Fw_PoPf2$kd#lHGn5Tt;7#dS&EDB;kh|6G=fBaySRw_^6INx+sdQ5r!jkYQYaor(qjpYf1L_IeHlwlqe7K2U06p}C6k(5wT6fw*kDZZ<#x47&oc-y)C4{5(?;2>)B&+dNj5WStP!Fe)m{P_fVUUitWj^*=0Tk)a4Y!*uhDM$Vj7E?2ef2LT3P;%aKv{42H(r(8UgI&*BD~QMD*#I;Z^}*ps0{A0!w?zT^(xZ<sUKrX@d%Vh;tQpMD*3xx(gNZq<f*_o-y-gsLP3M2=2~TK(RcTA((;+0xF2NamnDtT2mrYb;~X4V%C5~bg>n57wRgW&P`;EaRe~|k98pz5^x8!foE0Lt)P}liYf4lBa15*cCyAi@|sR{#0XFiJtNdYM94TGbLIh2{PunLCc0Mf?Yg^y9dl65iLME2F6i7mEP!Ik@K$3YUZ)R%Q4lO>b)1U3CyOq~>7d6kC_i+7wZ4M|R@jpuMv*<flZ6&Is|J(yH5b<joJWiO7h5bL*ILg7tFY*1%vW9^EjP0Kt}KFJkFT`(F@32OplPYy9banmRfdSb0b?nt4o{z$dSU9slu~flP<<Y=?Ml7UvTa!6NPUi|yR2LbEt$2i4iHu7zsA<ti<|FX>s|O@>*m)R*Kci}{bKX}A2+W3O0oX>!)sgDu5_=wwQ>I3=0|@-qs?Ef7meQAUw7Yl|H;FP>-R2ge0hHT{^j+5{C4y2?{=@e*S+{IdL<;@ppL$LX!^$>X@7Wpdgh=)Kc!#4fPP8syfXr}D43_I3+U*7*=i=)ulTzP@c*p&%IMtef8pP!==*3E>l}tP^4L#7LH!R<Q2Rj?@CHKzLufF#47x?of|44r`AS{V7M@xFdhV%=X^qM$ZqHYp`jYAuGDHU0R7`bR^Ww}3@*c_<1%MdkiP*%Ak~HtP=QE=k9T6{cl-shR;3NDMhn_@ZZbIvJDtI~aakz=pCcr?_iC1=;DG+1qugybUIO0DvNB})D_Noo1<ZBv%CKadY16TV{ZCzWI&nhUhrLC4h_*tV=DYmS5!?UJTRpy99Nw5jCQo9Cv0>knfUJKU{zi`59!#eBMee{qyez8(1kchTM((nsw_hNaGT!2<9HesXQtudflYtZe4OE9|*`FCXTp&^J!TlhaTM7VOoqT5$kq^v!fo+hx$LkG}&&XB#spc%j5O&?$(erQxnfV>3kxlW1aq6-j90I^`UTjks{W<Xz9nW2ViaZnY(Oh;$>t*`nFNEQrP#MDAoPb~n5DcwkTYi)L-r41}d;UkSY$}Rke4s-ZzSQ0IPiiUKP?cmxO8uX0nC>JfDr-atZfaB2_C|%S-yTX=2@0HU!wLl+cR^1J{#t6pQ1F$Otf{Ug;7KnP3qOzw6;u#de=cV%<2?wP>yIFP();&c3!Ilzj!MA#&(S#57{3wlb(2PPtcw~9vN8YWMgaR^_VCtK~twhgJA@>3HY||)1?Iuo21mQQUl@`@U_{}-JC92aOtXTc1<ZNytsE*H}i8V9-!i+uBBKMYIo>&BR-p`lj%?{0AD5Z4*mb2oZegJIKwxOT{_P%YK*a-Jcz$1>+OALg^$dAt<sHlUB37zm}AXKMLoT#gbU>KDvCoLWT`W^->9edWcP+pT>*0|HOlf>=XTof{zfW#F`<wH&IpAG?8;V4#VbHLP#bxZRUlj|qhWz3uup)~mm7u)lR(*mbntpBQ1%<UU@{nC>c4?RD9WG3{epEtR$%UHQMLylne)M<geFy96u=x<`)<W!GxX8;Yt(-4S<eFspg2k3J>*)pxZ#EkJlM1E5IkZdh~(x_Ja)+{=nNRXn&G)t@0RDD(WB19AjMOMU~vjPc5(69?(DGf!wS5T9Ng%Xu;p=sacmeKfgTf(j*h`96tP|@;aWFd>Tf%=dtvcuT==vUph-hA@#TKCEq-8Vl6bE<daPu(kbLC8x>4D2p8!}w+IH(&KGp6{N!+k5v87*rdV?{sh64_aw?uIr<HhQVSe_r{s-t*<w3-rBtN37TJ;1{zyvX?>L48B+DqIvSpr8u9W@sg%J&YC#H`M8XLYYfSR<b_Gh<as@0HsKy5RcDW1!$B3#3Zj~W362n3c6sjm_tuq8VHo)6}7VtZ|-o7D-x*M7@Yr2GBFvkTwCS!oLxy~S=41dwyh#tZ#@lAinw1jtPT)3r-cp&1C9cialTCPVrwV%OIpJoB5$;;huv|HMQ#t9qJBCs;5=cS65pq)cyH36YSldKN-Lee5@VEdeS09?@U^D9nuiO|s?&7-@yK<H%z2z_EsgO<43T0~Wn<JJx=zKs?T45Uk|(acrdWmp8DCOm3*tEh0Y<S5}H4ol1d4O*!|okBb?p6#(siStcGxO|RP7oEDXh*n0eQmtZ;0%IUV&%A=?FIAf4>CqLItPFR_7+41s@=R1S0qSzc3mkkWPcR9zp!1TBvy(I|C8jcxQocbjC26D~Hgl?SSt?ZUMh!#MB95!8=upcLW@A(xU23ci+pPu<ora;{lejrS@=OTuaHAxT8ueDCu1ONXhj~ziU?B^1X2E5G(~&$-%97TSzv%4Vx1SA1R@D`cE8V_SS-@14f=o2Yb0z5ygcXw{Z5|i@jOopTBJKE4xL&Gi;(`5!DEz{mDK56_O9ULtlTDeK7~j3u*k#~Hj8J3@W1Ton=ArUZ*y}*3=Q#l<L)Ai55$_k>ll(0cvm$8?$_QHuXK>Un5Ijp?$b;H)mTHg0N}_?2h4g1u+<uoZI30=Ca$RQWZ3JXgK+~#)TD3G+5~$RqjRZP_oFqbpjDY?{7{kPcHpPlgTH3ca6?Pa$!4fvmP3d^9P?In+`Wnca3I;Q)jTI7q7-7vPahY9n>R>~ZlnqKkPIKvm`#U6Jf{vs}g!(Jj<fMfJ|4CCHE&S9%{dK&^cnO&xFp4<re6OhadWbF2das7(7s&;kzvE>vQs)&l#}!f$DkeFs^g5bkg8HP->dyc|LPd>`F-aIJr>S%aUZX4`nMK^K<ocEJ01u3+pNwa8P*V{ml`Tv|AjSX^4nu`uC_w8YH07QIj`N{uT4vd4hCCuri@(-o&LA~8Nq@`+3L|@d<6g8O)Z_t>Nkh#h==;fP@{rI16*;aZ4-G=9$cbw5wpHc+>hd6!ncfRtMY#%Pbk2<e>aSn3j-3>3K)nH;O>Vp2#2_hsRcw>m-PyU+4$P|B(K3|N3MI<sT5GlGW&m$1Z<CF0+nq^c4IXq%y~o0;h^#6}G3{Z(3c&24e83pdl$|Ml3wIo@rNZ?V_Zo>8Pf^%6;-%A58>N~=*{CWP2}NK=_*$hZ$pqNNA1Z|e6(m-TczrI}*u4Hc6eFLEs8!h!A{a`UNzvXaMzd5l9_QDAF;R11h&V-9GXP~6YEnzS;YU`spCyVTlnJAjUx3v$R@AbRj8rrXl4^?rmWaUXmZ~7nkAXTmwjr`vakKaNM!SwJg3XfHqU3#Qa&}G`zF}h|j3n{9w$KQg&x$v5fi@@VsW+S|(gH=IXfwy4=P$#St^i|w2Z<J}hK^B=wELapStNH#nh2t6QSOcLF4#^un)Oz`wuC+c{-FY%xU$-fCHg7MfLfD64p0#p!1mi^n55Kpp1<Q1&O<R%Rmd}v6f@I;tOCcRxGLH>B|xOG+d^=!U|w=^pbx>zv=MebFnK6I7$Hlll@t$6Q-Asd8N;R=R!<E^vs$tyg>bvR;yFzl)2sksMto`r;un>8Am0NkQKZ3A&K#UR`df7Jr@xH^7_2@aTTx2{981x-m3W;;vyGd=*g>A06~Z_{kILbx=a0_(gxn}92GvonDHIL}n>-B@q?Y5)+3~q_XO$>0l7OmkC^%);hS36>qGf}vm4W|leNvPtt4k3&m<RY`*a=YL6j;AeT_%&1>WbgGUqb9E$^U1i`9mbMM20*Ruazf5R#By1ZXo4eE9dbmT3AuV8iYFtLrg)%7BPAW1@qE3WJOTW)L&8YdxZ$<WDA9!h~eW3LM|lOmI;ZP?K%>l3|uDoS_r$27RZM#iZKE^nEIktbd)EUUd|S|TVK}27g?0?*m$^riU{+zmQv1Z*Nv?Q?`_`tgf~rX+_<oQ?-K3_`)_Sa#gm5@H*bEre(z1p!0O(+xB1!W^+%UB9^Lxqsn=z681twL)Lf?F=K;Bj2U;W07k8_HRZob~REG!L-Ni;t`9{${MvK|Oqlc$?@x$kNEW^{4H+2X~VR0~?fh<nDDr-?6Gg!ltSq?x*nxOIp&UNmWiC<}J2xijZ*1BK^$()k@P*-7w1d^ISYS@|uJ;$vlLvd?R*U;R<@`M-F;crk+po;XON63BrO>iAGHYYnj%h*t|&ZL?DS))?V$OfL`vLO+So@^=jiJR^A@?Z|i%9MCJaI*Kyzrdj`B=Bi`xZ&X}589j%8x=1~N-Pwoyp>E{s{d4Uq-g_?KGM6w8;F#^)%&R@bxi8+YF}FH*oJTe2GuNRhW9L4mL4^bj{7>1I*(h-FlP#*0lp@~iZJ38^o45C0<MaHP2BtxIM4!b0|>)vW5qYBmHLuTTNE5vZ|e(H3MnkpbL<eh&zo*3uN(fV$%qFC=C4!49fcWHB8v}yVjumHm6&!$>a#2xVmy`pElCA@7iX%hQEe;~nqCFdfOU?lImN{aahu!^nHP^TRvz7io)GA`A&t{gYbDyJCb!AqE;z5okK``44zZO0l$DJr8;)R!X{YwzMHBIP|DhD{z4QMNa<jdRd8uE2(EH2H-i`BHr(WN9`}6htmyI>iO2@zqSCpeyZsRW6H$U&*I=}hxnI{jgZ{EJk-|6+$%UfqJZQg!INIcp<m<>Gs3b9#4ml;{Dfqj@$D>&?uzQZ_L#n{wBrNtXrK$`e6*u=hHgZDihK<=w}tyKe^^TtY}T`d{&6k=vazX&g7LTe~Z)gZyZ()D3;S0CjTdtO-vq+xdBHl2c`z?hLCda<tUc=?6bDC-|ZfGUDHBu(YCYI|tdm(9gitLaaUjd6|!Cew~Nz>ZEe=P!=6m&b%f?T?ijD|J+c$JT<Ko1L*WYPTaAWzfLe2`qB&!seq#IMRB31K+PPic%{GMHWz~IIR|LFQF_catQ8;>O#@nQv7F2gfr09tfZYaM5tz<FG!Oq5;xFWcPIN+hnPM*FPK6wmAaq&dGl|dK6!YJ95;O?3hm!T)M5?e&gZ;eb=_uWx3SB}NHm`^Cc@58-H$CvM7kl(yNK|M^~7YH#SHI#cysHEi{1ahH1+PqueZ)zg1&X{{$>5%N8N|N)O0>lh{axQj@2p7HoZw;BT|Z?faG)$&|66f=uwcM(e{ciPYJ0}Qm@LU;8EOA8`FkGHKDMjDYv0M;}~4Nu<`H@-HT^79^UWXc?&zg@%e@B{VS0Mc<Xu|H_ag~jrpHtxWTOGLr`3Fw76JnlrrOur}ym(-r<IKjGm~i?|IN;Gqm(`HPq>HIhE3!g{7`ZyZ1qIHQOyA>QKPQufQMhuCQl3#0>~O&np?o9~T*c_UdSRwWWH7CH+<j^9@D@v3r<He)D1PukUO=zPfqyA{Yi43CZ<nXORY2?e>_R+&w-%r&|qzTGG<jWt=Egh7#4PCKm*Y<=b`C5i+%k?_)G6B>6Sa{fe=G1I`X+r7qMar9=aSM%&Ayo$#hB9JUC;tTG6{M~cQUg!Y=ERET!Nns8XxC1o>3Wg)nc4UHr<{C16#_C;2+H186Ua#x(XTBidW75^&L(Q{VF+NA9~sPY2+U0*kQtE?G8!O%7!Elx@$02Y3RQV@5TktHJ*5XJBfn2Lx3)}BlV-e+ZoErVyZ9O`62s73-o#;$JwX;&0dG*}}IFs)WhprI>;?IGj)NlQ{<BshYVO#RG8a*0I~G>Vj`D2738qS|FzJgT@J;+Q9O-jJ(HP+e1OBO~g$+LlqzoE^_S>*UIFYy0<hOsO4OL-{0T4o(5j&b)MZ##q1irLl(Sk#gF5>%Gn2pGL9t&bf&E4&`Qy3<a3=U;$oDZ5tJlV5c<hVkO6PW{|;|H9{W2h&4JX^wlXtWEtZ^Mk=cddg!X!wJjQinbD}@Y6p)QYf6gpC&3I7kSgk25p1UBtkNjpsTalyCQ>;D?TKXdoS4ahDy-!yi-;^+;u3+p=8i&{CliP9dz>b?k!nuvs*P+qV2_H(G6>p}hAOpblQv`9TBGcnM#jvfG-z4Fo5m^ahUDpXJDiaDs#=+hiaBz+qy*e!^V@H_ls*wUizkmyZB!qbSm+Dl9l|niI)PbD+9JFd)*lhNbaw&v_?vv@yjM4_RK@TEeWzV@;~4UD#u1@r@Q)ShmS_mnJ?IRV&+tcVBNjg%Nm1#98YfRlN*p54Wm$Cr9e4Bg>F$-cK!E`j(EKjqMRYjRw;m^=pjfEq$J=#O1?h(+o!=m?#Gb~C?^erMqXsG`2p6%;?;@GhYWwsXJ#u9ScnCvyAul?OQoD(gJ+A-?P}(>`3wee|JAm;^1XV8;F>!L!fUuToMjmxlkQKwPLo;6}E27}6DPA5&pX^+cd!0Z~+KrYsC#ths4`04e@k|koi8VPUPHC}H!HXRqwpgc8lMJ;W-XDS2!haasEE0i8(<|64s=~S<uvqA2d9;IYONPO;8Uul}8vQ{mx=s~@H2WoDJ>$18c~wyx-4>H$V-vfd&X2=?6O$9qJiC8ljEhA<Q-+<*j+z38&O^ZI{R{Zov?5hO^|gg#JUqbYV}&)h-6W}AHrd~kTZ@tdkR27rMvZoIYj}(T8Rz&XV_6++!HmxaH?>vK5EbHmD^Vu*+tt>f>Rc+RzTaw~=QNfxPcOO4S`;m;ki;hgCLi0|t5qr4{)*E~MxcK-26{w0BT8DJofem13DQAW7MMU&VYRkQ<1@PRr@5WA+|H7*^Wfyp!;?E_My-yqCZKgp%|ge4t&)4STy6V{5u-B1OWO>wE00MS2)jg_Bxhlok3=Lkle>|S6~S~XAQ`(NiMa&nnu{=mQuOa4&&nhcJAsU9TWiv&=t@JF%hObuM7N&Ys#?%O`Vn|mUvFMNkQ$nDGqOn7j^ur%CiL(zXh<@6NuQB-x-6ZH%iu`qKxj@z1#P4x<5`-*T$`wn(Ne#xv<}k125;l(BoQJ9=a@v9heeCv9!*n-=@I>zDJhX^2-<Ull;VV6{*S1ilAx7IkXg7QtaYrx*;?_8t%2ZD)3><upgZ}C19^ncfa9Tpg5q1`J%}L`<N~p<WYYJT#IzjCQW8K_s9=_8zdB+=O|+#b(v&<7C~?kEB*RmeJxME5vaLs~Juk}X>uI!l#8D#>(qzf!s31{nP8}5(@)SegR6Bzxm=uTzDi5S)th%DE0unA`MJMI<godV(Xi0XVrBPpY%n~LXXANNU)^VyjLsPlHGg^~3=mp2>;X|epmkw&h#tMlZp7H3hBbt$v82*ubh<;!S*T^(mYw!}LxKL<1$#ElraidzrA)y#NCiW(eULO!4p$rg&vBk+FafnI5FNuVm7zNWwas79_2iFuq^m<XsB;zMdNjhmwM8CEHRE*}t$hJaxS_gVJzUY4PtBtE~bRWLny>|~i`RN(e-_=m}&*T2r7$8L8p<DTeOz}-x{)Ph{cnCs5A;AeCIQ8MF?q}lcaAPfQ3ZvQfgV7GcNHpnwFxoNu$uM%lf=+J6ViYlp6jM;_g-VNmCdli7QCJQ`ZCt<Aee_Q6#(7}k-j%EC_dmgN!`}RygRs2||FiMnx0}CzW8*rW7}ov$>svQYZ9MpF<H47kx9{>+c7EHv`|0|l8{LcdG22b+qwvOxHBg1uZNMK%Qz@@T-4zi{r4ZfA80)I?cE|Rj@reZ#`|w*ydQfsUd3@-@8bkNo8ykOqAa|h@*7&m{Pl+k4i7y?AM`5kr;cV{08hv!kJf`V5t@xTQfTkiVwY6+Kd?A`ots2uC<1p$4O1am`mr#nkVrE57U8p#QUFRZo0*YEOt!k)T{pdWT0qCB+zx6gAaM!zde(UU|?$z5Yg(t=2i)8bU64cR9L^xs3KdIoulQ7;P8&5Cy?ULKH@f^OWeY(SV&MjjykW&|X+R=A{(G)|3jm5F+K~SaMSjmzUpp)1m&}1d2+!80c<B}2H5E{gY6DCh26Vb{lN(X%leA?)#OAAX4Gc!T<g89dQG*3A^b}Dtd>AB05M%%Y>|5D2j&yGl*I(a_nNb)$FL_MS_<p<4l;I#9XHvV{f^Y6dv-M_m2;Nz|L&%i(xQA@^IF1kXjG0Klev&OiEq5%7YGe1&FzTk3Z8Y-b>f&BWoEM$!c{}s}9;tiG`R;rIq32S~94wy$9Oj4MUp>5$h&J3aN=&jk(_{mt`EWIkR1syj)It95@rG>kitg|GquBjVl0}ZwJo6mYTE_L6$**pCa-ZG*zm!P$dnFyy(3RhAhjcO3o)N^OmH6;|95-8wOPgo?ShwU)Br*A;H-npyYH$UiJz5LHpr?-CpZtqVw84v@7nbQ04?B=aI3~2LjAFn@n0LlQnW$}>GQ&)wuas4rrhvu~w*6&@~{N<yKFVA;x{jz)OV(*>5Z9aY*R61y_Cjp~Ma`pG!bMLR;`*Q2l+e|P3yzve^xv+lk6PTckk$$>P)IA(Jz9z-u!=jm(=+fSUTt@-#$I}n`&yN6vN}H+2@-dQyfPfd)4u|J$F0tZ(#>nB0%+QBsB1;8<S9a{g+!2)7ByQC?cgI+c>c1V#SiF6g&q~_5`S-2geq<)6ZiMMK5_35WY{}?;{T1>Mu<PkhyZ7(*K0ej^;L`!3R@xhWo07Us<#$Y2Bh;I~en`@5l|2z`#!7QtaOLpW;%PMLu^5lmGr^;#E}^lW+Ri?ilv(K2P?OGJ!Ekfi2`=DPq#*33M@o5}f%ie+%?#HQk;t+NG{z!eojjB?LzTqRL}VFLVDz2~lv343kxjfN%0<n>Aoo`k$f|bx<yX;;7#X@vr3HEMBry_`mIzM26A=<@IGzSDuX=xeU8Ey0-I+q%Xj*dV?hG}V*vpemK3yqobUBw#+isGQl!e)Tec6NoC31?Oi^2s>UI{NU#`rYMA6kNynKH@}RyL?VgtDTMCYIKy;Pl(|nN_@-sNrP9akE)1U(8C?c9Pb6EwzmE)0BHAzP3kbJlJU<6IcYdiG(nf#EnV?`3#sd<D=O>HBaJU4Ub5G!o%*vE3t$;(8}91J@uURIKyL7K9~GnVQ6YdQX5+8`nBGT4|+GQBgGjAE_~fvzXQv`SmP#ixg&R)wi+7iiicW=J#ABJRs)dabcyT}PAVK@@^8=9V|$OTItv+l;%jj50xd_PdNvv~v$p4#4R4`_*I9kc(!}b(kyiXBSUv2%k58;L*HIYE)~RcokI$;ZND6C8hrwJP%=!1po5WxHc#R|gCO3;#b+1aIt4Ki4=ry1hb+A^2CrkwhMQ-SES;<7(b+Csgy1!Z}OTw;9(T}7ZNcm8y=`#t%x1+rPQgjAEICI0?`O}@Xou%B)gF6rJoH6wX7OgEBYM8x)yB%^VZeT5YhuZ4oTXLsUED{7J%OkK|Ha<K}8=J7PPT!3$JoU{Leb-Bvv9(jOGQ0GXPqN0Y(6K8C6M|39O7f#bkH*rD$4XW>wSDJwNE4K#O;x!16+%ZkQGMYl=0kV1fnBIJ<}*z0T|`G%M3>yvLe;6wmmH%xX*4N7l|e<suHjgss|3fxjSM^z2K(gleqwHPR=as|a&9u}b*c{D{~>stV!BaA4_q4kA`Bsm04riUT4nQJ_Qv&*`E31!rF0Q=7{uu3RYXk8*<{Nzv<?g|qH-4X2_3IT_fHkd=JLQP)@tOI`{&S^Y2s?AH6?LDxO8^o{%7d*c;`;<(yi|OU$5UgwR!e$y;Gm@j2^f|D2<4c>R8ee%}ae;-{`Ul8obo~^1bd24AyMk{VS{_+%h|nB+yaGK`3TNt%N{Sz`%o3@pMCn8<`bD$axCsX{b7bhZn^pZqLSU-w0<!6qGrUgr(Kj#|u<U8?(EAS%3W7jd$LcF@z-G9X<6;_=;rOQsfwxlt`k<E-;q)?6NWwPdL;ichd2OflEAgvLUA|4<p5c!}%g-rYMt1?3knXB*OW`jo2A~bc~5QNl%_Osh@DK?h)<8o5A~O#f=eiV+#Cgk?CiMQ^#n_^Z~kFQ01ca%`H&+me^qf2%$#Kqr7qX4m@P9Wd8w3IBNSl8G$t0pD4Ti4A4ZYUmHP<jg?C?Ws#!o1!EYP65LQSN^OtARU{V=80_{k=G&MrS~BLn#tI&<taMFMWbg!(db1V`nkXh6qOO-hGr1jROg@(t(Q2a0sY70{7}zxGio32;8s%G6@jh`bXGZ)E@x8TNj!)g7uiCj0oHDiQ)83OdNS3ObO^p=D1g}_6c;9ZNoOA04flm6*<>=g(Y~W=Ht^9L+n<=i83JLZc^t=!c4w5kt-4Gnv5v(#62E;;>79En0-K!USUw&#F<FVm;%XF(b=?InJbsOLIjd{{m)_9nc&dN$+19_2#mAZkdMrYQvm+^)R=qZm0Ecvgwb^fkSDoG!tK2FAD_}hV*yd50iX8Vml5vu>b@PT{5qeoCx&>fSBZs9gfc}g36WV8+4(q}rG!>h6<=LT;a=WFhWg;)tQykcQ3f*NnQu2jRTd4yk;EDX-J4Bu>lXA^HfucU?!{0h0HB&b|}Q2N=WF>pKhehOIPx>w%&=c&`ZbMJPqe9?RR>-GC@gl-L#H(KzefZfMu@Jxu0&qOYx_=ZzJo@FBo;_@jw8oP8Pa+F-CY%m(3AX%F6lxnTUw=u<JRQjSD5lvz-C)L*+b6dqRaX6UxG;zmfY`0akJ$)5VR<|@2krkGNwv@JSAYBa3<La-@%d0ZIVn5GxaCN3_A3ZjG<hkjo<EVPljRwi`#R^eKKa&Rkq#a44M^dpz#a0zJy1BJR9d927gEp*U>-hvjWa*v0(fjfvp}_0Iq1!xZk>3CxY}L%h2lZR99eGP=I-cl7zww30Hq>J5gZz!bE-Z?2>lD-@!$Xkp>h12EpAR764&%h4OFTI08=O8>t<>=NBRstoNP*XO@QVXr3<y1uPVE)RnMOFoyThopVVdDgna3;G%55L@NVn*;@kBhwt5#f(>-Yw}Yd86(j`?;8PfAK>E>J?<R!$|8#`-1on#hHW2~0sJG56z&#Qku=Q0O7}q5VOuT8HDqC78%!3!1hi0~XJN(^D^=IB5TP>d*;$CfKd!kOb3itrR!mYIVP_fQEUvv6PkdZIIjZF0PQ(#%c`&S&B<2*qE8(5K7w>!*@IcBBn6F!xW39HkSHln`=1%>-R1jKX&I0&hO%9(j~u!9ZpJ|j)>OC!AXMnhmM?>KK}C5i}s8)GGvwzh=q6Wh$Z?4%)QVsc6_7z#u*R_jGiI(tiobH2Hk?y6S~qp^J(|W$9iAPNCQtEo)h6I6>vyM)9Bqe)xCOIQsBwM^LpxpmNKn28HiogjxUbJ9wv#J)3wP{h4uR9MPpOwAj0NC0w|!^mD`)YMEA|cgDXN7ak|p_qc;qul_5{ay59TpliqK>GQ=F^IUi0(0`*Bi0X=E`;Wd!Gn~$$=e0~9wpgGa}Jn2t2A8-;h%JR$SdtZJGL$`VNEj%FP-8)<bbJMA1Xfu}a)IIm%*888VKX|bI=$-DPUjtNj3SrRj?c3}3PO)E#Q?A)*p=)dZXRBDSl}V}ry0=Z`zUq!bzStgEb|cD&L!`^8T_NW$4PGocvkeK!BvkorBD6weqdCR+sr?Eg1G4Ip7S(9Fuij<KP>4KCQ!sZJ8-Mu+Fb}88_0GSyaqFgV2!8k$^30iY+$Y838yEhBdHS+*Tc;m)&s-8@x_Y^H_bwiyNLg?UXN*OG&M!l|7efanZX*;Vjx0>~CC1Z`xJuFnl>pwr=n@tZF&b-7ZxeiZ|9W?WOO1nP{%Vae+CJLbBfwyS#$$+4K8>EDw~fqN?eIK2jyb`V0L_ANR}d#vq$S;+SZ%tItCqLZOur<p#0ZYIV+a#B$lzj;fnxsh2e3?Sz5V{??c0gVRI2Qa=Z0=~Lz$Y?aaTrst5pgfeG*IuXNzhIpS;k;j5xQqrF?Er-Q2#)Ih5L&e0&MaSUNH*`HG}%D57y?w2wlXe1_=ox}@e*NY!`KH_{1a4tC95y&O`D9|YOxLu$dpbNF_!LL;=_M%f4w-)y6Ha;A>Z$Yq=mGz@VL@%7(rojQd$%J^?g$w&_z;@-GeE9UzB``x>rinRILpLQSrapTNqpmgF1sGt0qf5Hf{PRSU>0QJpjM)lb(vG$CV7Rl2@SUiuCsU74}C+%qz67l+n=_)&W?im=RUx%GIN+)G|VHd1Y@`wkrz4`P5p=KqW6T&WVQvosskl}pX!~3ua8@_<|yYyy1Gn6x1_10J$HAZ>A9d_F47{mFtx8<DI)}THOC$$(HS{Mly4|i|IH@QECJ&x*(KKe<S<#?X8$}F$DEAl#oYH8?NP}Wq3`{DI4aRn@*bk&-wJX9W79?EceEi}t%kQklSp@o9#lVN|Wbl56r`b{=LkbXNjiBSx;7DuiQ4k969{mH0UDPDPNCA1=({=vaa$@5f|pEbqhxw+?s<C)aEtU9NKf?v&sS9Gfm-b9hrPFajk^N<%@dlWifH##>8c!)8|0NM|#O<<Yot|+CS7%t@5x5?|C`*Td>^{RNrp-i1W&YyfpIp{nfcKpJc`4VVzxv2%0?#_B?X8O22_3zV1P5`;4inuW2@&3EUcA1@oGt<dJh%^H~$yR{hL9loKxFt5lHMyt5oU<K2(wgh*2U-gqLf}BVb@MMBe=+$ujr0?vg;-p}9Uch|b1`bHQwl$9Fmf_$+t#mo69<pcShm{DsymBJp<fpBZ4VOe_!RNKF*<GSUmCG}YQUmK(F&c2<v01-Vk=*Ak@+(sSVQ+pk}QdCB9SLAf}QEKI1{X?PtV=s<JPurNEB8^|Kla%B#7U+1$|0xIlW0d70_5?+&jkBZ+~a}$IQ_qrY_@BF6F}ewK1#*k2p-*STe1&h5$1@l3c$^P<Ye=2+K*w9nyt2VaDVGAfmVb;gYT%MypuqBxo!d=qQb*p?}Zo7}5O<cJR*B1t<%!pp&6_7PZ|1GflfQvESSaUBfu=_~Vg2Q#Hgt!)4MYI~kI8HpF_X*`x6x)JAyQ^8~{dY^^B+Y0%6G4U~3;9(N?^20d$ba$hi(KnCo<Ahkd=qiGO)s40hwzx`8hpMUYtcXBtP4R(Vq&mPIJwE}->(^no3OZA%|8N;VYsXXe)nI%L41d*5L-(`*9nY^Uh(k|D5$85ymrKFn7Pr0yE{tJKxwIf?K<Rqt3Ay&*r&Ro{+=;YF&M~QVpsVfC0M>H2#v7SgE&QeMoYgYsAab%5Il@^Cm@@bTzgSirhkdG%Bav<dxL~cf>+XZAENKnA)n^mFTOE405PfD1gTa~(8jAQlq5_qtI#Z3jIhmwbzTyf&+0J<MdFEN+GnoJJ@`u~jg9WWjp+cC7@2o9N+2b2aR6>q~AhKggZ<)Oq@p3kXc1n8uFo0`PiHK8em!IZbq#<l4fy_9#j;&*60r4-tdlzxlEHIIUZ`5aius0Y?dP8C;Pxhq{zsy#3F$I`|h8%;g><&#-jFo8h9aV8~U@90-2ud;ziwM9Qn`N~9ny4ozr`-P3xN<+Ivj%2RDL{=d<j_6bd%ts-J<4E+aB}Mf%s}Z~8a83+DOX`1UTG6$Q1#@6e#N^1~<mgw?cVR{5mwu+)t``d-0V(J&&&*0AevS$RLm-FCp;NYu23_w>2T8KP5?D~J815uK6)gP4W(~4b2#nEmS<D@f{WC~d_JtlJdvdXKnkSbZL~yP(fZF14Ev7k%3Y$HgQM%JRMI!p8WmAdSFYkJjaAG*ia*2E!R7*jc3WWV7gq@H!HHV#p%ag<dvnkiCDru+0O;}8c>*^N#Kz8sJdwfRlLnz>3w7dobp6r`M@mIkE|5^V<FOF&3h@NemliEcNGvwWzK3TyV4gq<n`M(e3dKNsa000"
OLD_SERVICE = b"ABzY8000000t3A~e{<Z%b^qU|*uXP2PCnu&B~H_`&j~G27HdglNh-F>XfU_~j)(FNxB@s*7xrf4OcKeCEjjUzHnI~ZX`N&`ZIz~p?O00lT@oEB{pEgyzV~()SS)}eX{WVk#KU6u?c29+-|vNC7!UvO#>1b!cJPlM9=>w(;Jtr4xb?Gx-+u2;*Izz(_csUc{A%{b-Px<ZeDLwD!+UQY{P?{`*Y6y@|G}TGf6Fk8S}zKR%(i>uWE^?6&HUjgj1uMqL6|s+9|m!)Cfnjk(Dg(4*iVK7`TOtUFp$5)SpJIp<HS$<B>vEozsF<W<x`KGq(AUGa^5v~<S&xR$Pcz<`{qk)%l6p|%V(cox%^y%Ed`TWhDS2GR^x-5(MXQ)gE(;p1JAX0y(or-G?@4SVAI~#Al%+o=GpcV8=Jf+>ymf_Z#!~^_Sl!*VlQy*$nW;EUgMGLBp$$XTrZOS@X6+Hq6QN`?6@2Gqr}Fg!zz0D#kFb-{!78eUbO3Xy;wrr5YM$5pvl?_!luFD8}(Z4xs}ZeSDv-km)BOIPZ(QXu<J)*U_p1&SY2Db{Ot15x~+6G8q9E=0N^aRk>?DX;m8X*o)ZB$(QRpM1%UMoBzW9vHl2}gZTm@o+_Adhuo-!yFmCQEFsDXsZS~?sd*$-x^7_}8F4`L~{)>x?wa3_I-(wNb)rmbe@Z*G;iL-<IFp9hd@glA>KZah83tY18vCf44Hdx^8c`&5sM{#1+)|W3XFKsN_YfH~91N_qf|I*Tnw%UyIKDk)Cva!5w!{g;mcs5pHfzQGM7p@T5&i1|TPV=<2XaK~GD;GEI^D7q-pdc$?6^{prg{*GWHa3?wm-QaNaKHh)!{yb@mGdiSmo`^cFK_4_;cWRmzst{;7_h$lH&>Q7N@o50F)UVes?}Vt$KZuyyMAONm)PAf==s|w|I@;wQD+O!&`ILF<aUVF04TPZzyeN^MCo5X5Vk)~!U*UKoNze|ygD)jABntS)L9rY!)oH>DRWqE2lyjDiTGuC*G5j@j)8$q-UT=v7M{NMW83M(;b5G2W}RbN#MbKWxn`X;Wixe)JP=kv3TWg+UXTE79%Czb=!34uTyHc8CqrllQRcD)*v;v}v5DC>P<79laK3_eU_dT!po@?hEDA$lEY78i*6A~B&+Ew3%Us{3bpXE)9GD6Q4jU{Ep#w+>7nyhuP6hMf_<PPS99`@I(86)@6t?bo-iR7eAORnEZi9K(lE~>M9<q9O2f2M5fb67R0lXBmGr%uOVym{cboP1RWPAC=&GjW@Z_^M9Yj*qqH%vaRFE5?Dv}_GsY&;iscf6?Q4{(<a%cVzuI|w6cfakdB3mV2^Ovekl{h<@>EC|$4zz`q8i2#=IY|(DhB4Sfe(MBq^XcrqP%kpPpP(1cqaS~nSdk@O!Y&e>*p&!R22sqG%Fc?f2VjO4t1PtK#fkLdL50XOeUe_7&J+)B5A#4e=&9*?cupV$S{`CW<(NVByZVAAA(76u$3<$?&{VIVJ8;1NYmInRAp^Xgu4G>VX&~MROU<260IBgC?NWBGoEsnc#YUC0Cb$k>6@esk(W?i<^U}!{wsi#UDnJtd|j_<}uj1iGygoH=|0{R>xOgypzq$L7|F8hXUUs^i5y73}0&$$hI1GnFpa-ecA+9_=Sf+>H)0zsV_^$cTg07nlo*;>4cT?bxZ;$xk)L5kC{6>qgMD4l4=B${L^i~&<S!$JN6zCiSG41qM|i&Bc$EffoOXOeiZ(})PYQyazZcfC=<RyUTTD2ytRIPU<y>MpA9t2RfRDi=41w<!E7*g%;v>cZ(lb(pQ07`DNROq`CAd=8^?EF_*e_bBXx|AC%J{9x>*jmdC??2p<B1)hz(V)hJcpPKM#YKg@x?4!kJ;Xhw8>sH^pHWhlKu#cnz5C9kJ)^G>0;}3CrGa7p+E#WBI;SN34b3<We>Z0;+iC|=ug2ghLB$4NtL_k<d9t<+t+_7gW20{ZYnn;1G$X-^l4E_fOoW<XRL0}pWfBM0r-@h@t{e7_MXRm$d;GG)}?tM7>m;ZeDn-AdRJ^bkI?7zR=Mwz6q949|=<<XGV4Sj({Nc7ttq7;`<vz!b#%{UnN!Hy~Tv3xHI93xrV0#N`Kw3)8KPnC(AO@~zRgI;JFvyXoL;NGp->o*QR`NiSeKb-yhKONrrc1j;kE>0z&5C74Y@an3`ARC1ObCPiAckR6hq@Ilez(j*W>a3A7LHoT08ox`Liu{hNq`Xj`w1n78O_1aADnB?hkPc2f?)Cg@G`GcnvnN2nLN_BFU~wc9kf$bMYPsX#C^iL-FijAn1LwqD-*1!LK>^?bBJDGE1osWJ3P6>IXrZ)?anf7(iUM9(GlWE@-$CqYWTDmP3DPPGW7LVTL_!qti}~URAT6PQNKUSv2l;b3OwNbnz}1e6*vS2<+J9M|ojmiXN9yjq+3k1OUvI2lX8K8jh45w9D`t`QCN2s%)sLl#WMWf;$RTtSjHuPR0|yLE?p6qYO<3YURNM9gKe25y_69w$8+W^{)Tz^0iw-UHvY-=C6aLLNbIYB-5fAweoiGt^;aC1uPC2NyHj3<Uu<M!i{D9QVX_xyS84lV&@`|KQp9j@snIDxOl=G0P1{Zy-JP_`&RgG4>Jbf6sT-D<;M=d|lPMmnBCF3O4COmE70b&2Z?T>b5x@2PpV1vUOIp`uh>ZG^92ElC>)@;^}o_LD~K&&MMaCYFM%aflkWj$%H3p*WQlv(0Md8u3Y1rBQDsZ{54(wb;ya$B+qOIV!N0vHz-W9HSXKlH$lYV&TJ{K-7CXf4(oxsJVXFbMZ-koA7#b)_`Ub?JK;s)^hC1qEkZ0k3soX&a_DS}Y42G>Sc!x+r6y7n-*N!LiNVz!jm9C&;eM51eL?iV+m<cmX<cM@4HkSD#<LEZlK0SYhA%k-{-(1|X=s+RkWfiDhpYOXH*;M*cSmW#cNvb}%3UKJGq&m@52+hZuTfDnUEj6)cOgm4`)WE!di(uA;0jdhMl6JY#3Ctgi#zZG7P4DyegBWn<~ti_7+vwR0#^D4b@H32&S>islwKEJV2UpRXUh|G~j~|H=cUpc!Vj-k-huleTg2+wVQR`}V=jcfbNU{Q1j=Z~fB9n>hTOl!yW4NOyr~sJfxmlxIGKEo(3|6WfLirYwk%PZuLm$~@)h0!Asi5AMA=`{d2G5_*Fr{Qk|^E3fkr?LsgVtoz~_1*9sDC|cFo(iESnjLWc()kXa{Fg4>?H}naEJ;zU!p?L$8D|kuDvji2^QDQ`MV=Rs<Fw&)Ox+D@jl}l2wqvPXr2Vv}K3(JYHOz39B<y&4s-7FaG_q^_;JMd(*7bmyD{j*7cjz_gTTKa2KS_^Z0D|JDjuj4Uzq(V`Ge{7TDooLJ3>w`hYP=b~?17&+!m<U<g<`PkZ{TX(d4xZ!3UKOAcdvK1s#m^Ia&QC6kJB+&(aBhT|Sg&AMiEKRZypgHJxeIRJahArHlxpiZa7J-IOoX9$s==ahfXNHpA<3)zN&_ALlQGP8E;FIp8^@RPoN0=E6{{=j?Cd)J!08M;(g~?PNSlqiW?7ea%Guhv<J=ub=yhdrHC5JSq7;CFWhY(;{bXjyfr8ne*7>xeQ>nT_S&?OvtgzSfz|t`D;Du3Sk3ggHNK+xElFr;HqK3<Bs5T$pEn6jJCkzMLtQ6%^aKmItRKQG-OL2KdeayuaG&VI08u%{mfl3EHh+=~-pi7B@X?}?kE+1X+uyTEka0e`@qNSGZMN1;Cz30n=*BL?c9XRclQch|WsHqL$>uz!MwRd1rCX*M!Xa@r+(1&(GoFz1iM__Q;)TwAG*EKuU5HHu^9e^V07shBq03{Gs(7V_b!<uM$8d<!$K&m)dry~jg#2JRoC2lMBq^#HycJ;v&SNMV?y63_x;*G_!X-LM&MP);?SMq3SV}+bU{P-_^F0)?KQvX7TGc#w6WrI#-KgmyxhJ5F;X3(VIscVf6`4P06`l||g%sYHOIV@z1M6|@_gd<0%MZ|gxPY~M>V{CL|!S|sYPJyD+Y<)yT%2;I%sb<z4UnzVxNs^N$X|@3or;e1F;?jlK4p%ZQs`ex3A`ZMCfItqnf%9Vom^<gwa^d`crL&ZcPWzvDSwUzE?PBE7=Q2_3Lkj;0-F6)Pb_~7tdGJ1-CND=+Rd$(@J27s~09k!v3$@ZHQ9x526LGLOeLLfL0>njCj2Lpka#AefNt}2?n_$g9!(=j|gpww~v6Yd&3Jyi$t;)WWVo#&&J*g%0$bQow+2d$X&YbY_N;H_un_~Qlk&@{O91pIdmuPs&(4UXlH$7!l))TswgpXnnQ$q(PJY5no5+he!(!X-Pl8)#Weto{-L;aF4>m^vfq?5BPG_gkQG$PY8cK7-4sjiU;g4mXpBz{SkxbK{K^2_D(h<D{GUm4Bks05&LMhT3QTT|Ska}wvAAE`sqSY{cfJYIj<8KUY)130o+%P1Mr_nbk}7mMT30%}1@nq_m4%#g+<J(TsL6M7Dhn(V5a*?Kkik>$pXIt-~=_>ZaErqe=FRwMxQka$}6P&(kGDO{1wPUmYfJw#@WCi$syiP@4eI}Env)3K!B3+cZ?{J$ZOXj!~x=tbL}O<Lb1`=M=+A&OEu9k}AQvr~S;aM-{Z2i<;Z9u`-i$2-89!?q&S$7Fbe45*k5oeAqZyI4xo15>E)xuUR!m*fcrkfvZt;Prh>4NOFAh-1nozBh0^*J77F(3p%U3xFMPF$QiL(O?~?yJHpe(^W(jMquhUw0p*WG8uU$EVXXgRCr+9(-zx@hG|K}Mhp1xxT&a({oyYw13mMVNzV}5(*rT(xiI@2oTjA~eDQczIKW)$Aa8kYb%$eXmSbrQFz<GKZ*QHu5i$6TIDaZ;^1?aNdKE=U8rOoo7XimC9$MumohZg6Yy<vop3B4~kl~;MD>rtk&ABCBV_F3J*Nzk#DMk$eqL@xDogN<*@~C<tr4NuMTM$Kt+a$YX4X2V0w)VUZ`ZdrA&dlkr{MDCFlfLA?QXu^>^$n_lU?(&^PoMcqYZ3lGEmW`atWuv~lIyleuNQG_tA+n=wOUtopG}~kK+_4vYmHmY=05hFw)SyV(<Y~oQH-(Y#idBFlNL~l7xJ<9(In~fNFY}sAkMmda8{}Li+sIch}+&ZuRBhV;T0$3NH^XKcF~X&);JY=Byf6!PRfvoLQL@I1Ir?96puUj8r)UFA)gKvK7zXdE`ers7z+#C5S*uOqP^(Bn{nVLZ9VU_uUiAtl96CD!=l=2OPd$k`|yj7|Ks@hIMM3y$LEZ?uzG1(8HJBxR1+<;!ce{o4J{Y}J4<`2hm9}+J1J5@NJ66ojIQnLdLI{a&1s}R-3e=LWo^087Zk(MebrDqLboGluOUX`@01O2IM&q<VXwNV4E2$iE6!jb6+4&@cD-)sdZwtWLBEk#_LQWG`K$ttoc}Yx!O#t2%TFy1V_#^qMZKmk0gaJOMhA;81Vu^^Vtw>z460+R_2lB>)naOdtz%8}&?6hb?5Vt;o<y#TS=4RsYbc>A<~U4CbE}y-@X@ayUBC124>y=PL7Hn7Ou_$bEdZ*mt8xR|{Euj@D?(j~m83!)^=L*T!!Taxdtih`yqXz{=OR47@a*h(G3#L2FdAl2SB5-d2GRthzzN!%?BLc12XB4IbJmBi+^nbd&x%OZtN#_D-;j2EtXu)e5Qj$6lyobDRH80_b6LKlGRZRST1>fWIrR(!>3QTLPqoY>T^IL87}kupSX163X^K!-Kt?GCA2Ub2Yr@O`xX=Lu!u1r0;%$SGTA(a3!MI2O>lS+f<EHAJ6;ZKI7gR*pOo69ts!Z3w5m!sw_xj-=e>DbtCFslVzGOx-kDh=boL$bDrVIY+2;3j+-`-cR!AuQ)R8x9VNU{QkAX>&iqHY8u9#dsWO95Nnbhe4+z@G6Q_Q8r}T+NmBi+JN9t;m0h#h?VcedJbrFFDtZ!ch!8Ed}s-0fVjw0T7?lmTvtJF&>Xw(yPHfInt-wLEZ6sh633Ec>#t3-ga^oc*=DRu@H2pD9s)}1uYZC$B;1^m+nf+-BkWUhqW?ho>jzPRG@)7O64krxK>)Qv(j6@)3R{M0TPmBnoU-n@CRdblxh{FkO8O1Fiu(rCH`s!F5zCRjTW>e28&GuU)T7GFFYR6a>ubvDgl;uW^u7341O#pw&)xl*BxxIG*{KuEu6W<JiI8mu!t`%e(5OGswY&vSi|@%(|_sDH+>^ZJG4pOpA?;E5S_)N&?&f<bMPy=?#eE6%|a!DY+px4w;ks$MpUN}48ivNIHD-yM!%^XWLUd)_|VOf0+Bdc-AqxGjT7MMDUrD}F-Zl@ijv*59C`|ni#J;)WYY5QY$Ok;X_=|C9}3DjsVF&7sa`%gvmx=?_z(XHa;{Ah=lD!mC@9Ryx@uOXBx1?>DHS)@+<1(g^Lh?O9q{TRw<X8|OLa<17y%BbEvHB3D=3GAlp5ZX!+U9&MUYbt5)sa9G+c07?uBA>Wl5y0u}C6u`>P~nmsuQX5=~51SGh({;QfE^^ZY&5wn$CPmMNwr%*r~_)qa)|CmsUL@{e(<a@F9dEkdC@|Em`poiG7=yzD2h(#sbB%`5!La-q~q+QU54URb3bQiG}T+))@IFZ6K0x$$DIvU&1AA7jsZ-iWL(3>Nn=luU*sSjey~e#am9$)v$LV`<650Toee@=TGs8bm2{)5x>K%{zA-5&x*$m`1dnZO0F+@=|~;L#{$bP|*zIYg9zY*@5E^#Vix{?DF~5^<}mxz9Ya^nyVJxEsp4VqU#O(ju!z4=n8Hy=V-K+bB+kVS&Ya_Jd=JDj<@?Pc6R9t24v>45GXT%?Z74x?C;Xo9297s5WJQy;pj+~vDlShCkXcfLI`c!CWgdZYynS~yD4rcLaaF}6D(@Fn23(IH^f^<5iSg*3&<d%a}wevVLZ#FMIuYd<)E0gYR5Dmj%zw(5~<KErBkik%rG=NB9mGbrD7UG#O(r~mqvdo1zyFOQw1G=Z#ym!TinL5;ke5Q`4y`a-D0V?X2SD8DLF#&Hpw~|D1f<_-49VJO>;qu%~Gee#XYLi%p|Z}>|`Dj1I33-lHf-%fICuv#V<FQ>en8K!yOQ2>MeTS0i5wA4x92#7zoNr!6|+OiW_`>%+EOk?p1cfF~}HFmFN_L*ijsF023(i^T8DE74a`n6zw?8`oP}8^eRnD<SelsUV3Zrm9%LPjR!4zx|XlU#tYN*3hnURS1^#+1mNi+L3PU#dOgurU?1f&o-pL_7rYJsofd033LeE5$Vn`z>nIBV&i4nHDH)O$$x)~qrS5H+J$}NyJPOMaWaO;_hu~?L5g%A^km;f<ZgU|!f;}pacFqXmCw$(8)~XC#c8w=zp+YnH*+l~;D`18`Tc=xew;z65{L>Yf0|E2L5Kf3H_^2Q!rkk<KOuW^o@M!XVa{kR$7DCR-jnpf2eW%!n*ZqsFXob)mXvXl#T1W5+^(yr@N^eG!BEcw`uoiJNUt2kqvk;)xy@RX*M-lZlPfE=zrJ@-zjF{$v_Qn29Eg+gft_k+a?OxhhdMDTc*i^ixk(u8jh+jg5Y+K-8?cTY9ow0FY>CBT~-nepU!zjDe?i>q9@na7~Sjz6Jz<A|Ro+U8SCuYWl9_K1iX}L!{9>!IoR!opZ==PQChN$D4VE6h#gPm%gGNwhZ5OD<XB~iHYdok%c^Vv+i-3|hUzeRv1K}7@_)vQx;%ct~X0x}D;=S1kXq*t}GP0ucDAHMqG!S$Eh`l9C7#I%ko2dSJ;mAJgT0Sg!KAm~K_<f4GPZBre!&L}BceJ)NRDDyP4oTi*#)a$9p{`0sdo$A=EBfe>q4J=Wn$^?p_#37Y(at{5;^QFUFT7c4^4XW2GLF!wEax?=c-(r5JTzsCKqYDzKz}Tg9|MPHn<>d7vuH<Bu&+_t6t)#HD;AwHa)hb#JnLdLtdJ4Z2x|6a&)LwC0WHj!8*6)jo#=_9_we2KTAn;R$S>0)FIKIm1P;Kb^s@Qx@s}Se4ikeyap<jh5RA1>wK^u@`+*Y2;=Ukwfa}~5Ak^9+8vlxBOh(4yrtv4!!s_V_tjW*`TYf%lQVL355`M-J*7Mh{Zeq65_`_>CZVJT9dt1e6@e$k(Aa9oie%aZ8y`7ZJeQE*xqEZXrV`02I8yjCZB^%~l$rL|r1dzKAmfFlV{KGg}PS%2J!t*hMN=J1|ji}j*|3rJuMhy2jUadP<-&I9)x3+w?s0MR;rMXdPbrYUD^NKl+upHoiGZh!xg{9r2oLTW0VAKbt5@a~V=Vm7s`qY!lAdzanjDRZ@Q#e8Xy7zK^x#a(BF7m1Dx7?(wiRS=~&{!`-gdLDhd&Up0R^@l%y`O%HnXK&o4>VTu_Iz_f?cJm{o(@%f<=tn=Bz5ZXb+jl>`fAi7LzCHW?tDoL~?ZLed@H++XeKNcIyMw!LJ^1*O*^TdhdjFL|HHucv_CN1`_igrc=Nb8FPP~kLc<Wa%6aS9E)6LE^-v|t?yo`V8;M4neo<?=~%<jItqVsf<8j2$h-n$Mkl@a3JL*;<Ox86T^`{uzrznk5^EmwJGU!^xEX??1XH4a{Rb9Vnad%^3lgIm9t-TZKN8)km(@LRu|-MBaV=;seUe)Zume!$l^yZsR-CTBzZM6VJ$%-Bv9bDZNu(d&`y#_bAW?x@n1zbKkILobebC`xBgb+qVJ!kxWnc*I!snhPKQRTV%I9x*;EW-b}+_tGT;x5s1p2B=XQh3grAeD6Cs?&G^~aPy7XtM7Bxll%g!lFY*X_}w2c*`9<M5A6%0OpOX`C@tsOSPWl@(Vd@s`tj`&q_}JXKEls{y!+#Wdv`d3hwuK!!B1|K;FETp-TW0RS;jH&<+XK{sQ&1+N<5Fit#oby*brsDUqeLvE|{<V&{YJm9&6`GxYC1A@|h!<O37XD)C2m(;1+#6QwF+Fr^(-bmPN{YIoT%xP@yZCx~p18F}y00b@ZLoOtW~~PIpqCe$<m#^4x03D4!=#?y%;aTRu&mDGF&p>QnGEN(`7Z1)0;brO8%a-F=eX<{$LMwYkm!znCGbMx8+%^2;hQD}2kDcB_l&0YHvADF8*LF3TRsOrynCsc-;GImNXw7VZTiW1*%U@nf6TF-WtOBk?IU53TaQW{wv>q^76JDk1T7cGmf7Zd240o%lk@DazT#**B=Q?|dCU@%|XQ<Zok2l&2~&ADpLI7!92SYhvM-$CfU?q?`<q4j$%iOTtzK-NBfWs16oUHF@zyBobXbYt#iNIf`>vq0$X46B@^^1>o!?K2X5Pq`llK%AKp_M*h~RR7f1LcUS8wN!Rct*qZ!H*RF?06Aee62o|J721gnxIra3I5KsjmM#a%K$Nn<8{S+nUcyTA~H{Qu38Hsvs&xzbPpU}6C@}P<w++h3a6<b!MT&tR<Q+-onFL8Ork13%xm~vC50<DZ~g%pxs%`B<7kwsl|%`=fpEh!OpWSOhv$*vPcKFBH)vopIdS}$3DYgtyk!7pH7Ewk$_>_QW?@QQM0JI>0%vz(5sQJRy}$?`c$Z3#t$arXM$>`t246jBYR6i{ejsy4NcGy??oM80>M@=2)z;-uE9;j#j)G-FNQO;XD0C~R1g@XBSj#ZIJ=BXv*Y7EshsDLBru#;_PHY2a1+Da|QaM;{;$_xJ~$;jbTLh01x&3vZ>)m3b6s%9V}f9a^pUXHNMSz{!_>T~)Ve=v*^J3L1n>d*BQ^u7h*8iu+#yyl%lryl+c~N)&tX4byK@<qm+rpz;fUYcFkHSQTGBoO4^tXpZ9uzk=VC&9(mp<WVEwB4q#o"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


class ExecHandoff(BaseException):
    def __init__(self, python, args):
        self.python, self.args = python, args


class Upgrade:
    def __init__(self, tmp, monkeypatch):
        self.tmp, self.mp = tmp, monkeypatch
        self.events = []
        self.root = tmp / 'install'
        self.data = tmp / 'private-data'
        self.config = tmp / 'config with spaces' / 'live%instance.json'
        self.database = self.data / 'actual-name.sqlite'
        write(self.config, json.dumps({'storage': {'dbPath': str(self.database)}, 'web': {'port': 23456}}))
        self.config.chmod(0o600)
        write(self.root / 'openbear.json', '{"storage":{"dbPath":"data/wrong.db"}}')
        write(self.root / 'app/__init__.py', '__version__ = "0.16.0"\n')
        write(self.root / 'app/main.py', 'OLD = True\n')
        write(self.root / 'scripts/updater.py', gzip.decompress(base64.b85decode(OLD_UPDATER)))
        write(self.root / 'scripts/release_validation.py', '# old validator must not handle the new preflight\n')
        write(self.root / 'web/dist/index.html', '<!doctype html>old')
        write(self.root / 'openbear.service', (ROOT / 'openbear.service').read_text())
        write(self.root / 'skills/local/SKILL.md', 'keep skill')
        write(self.root / 'workspace/keep', 'keep workspace')
        write(self.database, 'database boundary marker, not an SQLite DB')
        (self.root / '.venv/bin').mkdir(parents=True)
        (self.root / '.venv/bin/python').symlink_to(sys.executable)
        self.unit_dir = tmp / 'units'
        self.unit_dir.mkdir()
        self.old = load(self.root / 'scripts/updater.py', 'legacy_updater_' + uuid.uuid4().hex)
        assert hashlib.sha256((self.root / 'scripts/updater.py').read_bytes()).hexdigest() == '4491aeafaa4cec0af6ac094acc67c69f8fe18f34935148ab8c59d8df97e9c945'
        old_service_file = write(tmp / 'legacy-service.py', gzip.decompress(base64.b85decode(OLD_SERVICE)))
        self.service = load(old_service_file, 'legacy_service_' + uuid.uuid4().hex)
        self.incoming = make_release(tmp / 'incoming', '0.16.1')
        shutil.copy2(ROOT / 'scripts/updater.py', self.incoming / 'scripts/updater.py')
        shutil.copytree(ROOT / 'prompts', self.incoming / 'prompts', dirs_exist_ok=True)
        self.archive = tmp / 'incoming.zip'
        self.digest = zip_tree(self.incoming, self.archive)
        # Synthetic /proc, not the online process or database.
        self.proc = tmp / 'proc'
        process = self.proc / '123'
        write(process / 'stat', '123 (python) ' + ' '.join(['S'] + ['0'] * 18 + ['123456'] + ['0'] * 5))
        write(process / 'cmdline', b'python\0-m\0app.main\0')
        write(process / 'environ', ('OPENBEAR_CONFIG=' + str(self.config) + '\0HOME=/root\0').encode())
        (process / 'cwd').symlink_to(self.root, target_is_directory=True)
        self.fail = ''
        self.validators = []
        self.mp.chdir(self.root)

    def boundary_run(self, cmd, **kwargs):
        if cmd[:2] == ['systemctl', 'show']:
            self.events.append('observe-config')
            return subprocess.CompletedProcess(cmd, 0, 'MainPID=123\nWorkingDirectory=' + str(self.root) + '\nLoadState=loaded\n', '')
        if cmd[0] == str(self.root / '.venv/bin/python') and cmd[1] == '-c':
            self.events.append('check-supported-python')
            assert 'sys.version_info >= (3,11)' in cmd[2]
            return subprocess.CompletedProcess(cmd, 0, b'', b'')
        pytest.fail('unexpected subprocess: ' + repr(cmd))

    def validation(self, module):
        self.mp.setattr(module, 'PROC_ROOT', self.proc)
        self.mp.setattr(module.subprocess, 'run', self.boundary_run)
        self.mp.setattr(module.os, 'execv', self.execv)
        self.validators.append(module)
        return module

    def execv(self, python, args):
        self.events.append('handoff')
        assert 'stop' not in self.events[self.round_start:] and 'backup-db' not in self.events[self.round_start:]
        assert python == str(self.root / '.venv/bin/python')
        assert Path(args[1]).parent.parent == self.data / 'updater'
        assert 'staging' not in Path(args[1]).parts
        raise ExecHandoff(python, args)

    async def launch(self):
        self.round_start = len(self.events)
        svc = types.SimpleNamespace(config=types.SimpleNamespace(
            storage=types.SimpleNamespace(db_path=str(self.database)),
            web=types.SimpleNamespace(host='127.0.0.1', port=23456)), bot=None)
        self.mp.setattr(self.service, 'load_updater', lambda: self.old)
        self.mp.setattr(self.service, 'updater_script_path', lambda: self.root / 'scripts/updater.py')
        self.mp.setattr(self.service, 'installed_version', lambda: '0.16.0')
        updater = self.service.UpdateService(svc)
        self.service.atomic_write_json(updater.state_path, {'available': {
            'version': '0.16.1', 'zipUrl': 'https://invalid.test/package.zip', 'sha256': self.digest}})
        calls = []

        async def create(*args, **kwargs):
            calls.append(args)
            async def communicate():
                return b'', b''
            return types.SimpleNamespace(returncode=0, communicate=communicate)
        with self.mp.context() as m:
            m.setattr(self.service.shutil, 'which', lambda name: '/usr/bin/python3.8' if name == 'python3' else None)
            m.setattr(self.service.asyncio, 'create_subprocess_exec', create)
            result = await updater.start_update(confirm=True, force=False, allow_dirty=False, running={})
        assert result['started']
        command = calls[0]
        assert command[0] == 'systemd-run'
        assert command[5] == '/usr/bin/python3.8'  # real old launcher chooses the bad interpreter
        assert command[6:] == (str(self.data / 'updater/openbear-update.py'), 'apply', '--request', str(updater.request_path))
        self.request_path = updater.request_path
        self.request = json.loads(self.request_path.read_text())
        assert 'configPath' not in self.request  # no new fields sneaked into the legacy request
        assert (self.data / 'updater/openbear-update.py').read_bytes() == (self.root / 'scripts/updater.py').read_bytes()
        return command

    def enter_old(self, command):
        copied = load(Path(command[6]), 'copied_old_' + uuid.uuid4().hex)
        original_load = copied._load_release_validation
        self.mp.setattr(copied, '_load_release_validation', lambda *a, **kw: self.validation(original_load(*a, **kw)))
        self.mp.setattr(copied, '_download', lambda url, target: shutil.copy2(self.archive, target))
        self.mp.setattr(copied.Updater, '_stop_service', lambda _s: pytest.fail('old updater must not stop service'))
        self.mp.setattr(copied.Updater, '_backup_database', lambda _s: pytest.fail('old updater must not back up wrong DB'))
        self.mp.setattr(sys, 'argv', list(command[6:]))
        with pytest.raises(ExecHandoff) as handoff:
            copied.main()
        return handoff.value

    def enter_new(self, handoff):
        new = load(Path(handoff.args[1]), 'handed_updater_' + uuid.uuid4().hex)
        original_load = new._load_release_validation
        def validator(*args, **kwargs):
            module = self.validation(original_load(*args, **kwargs))
            def backup(root, backup_dir, *, config_path, home, expected_database, label):
                self.events.append('backup-db')
                assert self.events[-2] == 'stop'
                assert config_path == self.config and home == '/root'
                assert expected_database == str(self.database)
                assert module.resolve_storage_db_path(root, config_path, home=home) == self.database
                if self.fail == 'backup':
                    raise RuntimeError('injected backup failure')
                path = write(Path(backup_dir) / 'snapshot.marker', 'snapshot boundary marker')
                return {'created': True, 'backupPath': str(path), 'databasePath': str(self.database)}
            self.mp.setattr(module, 'create_sqlite_backup', backup)
            return module
        self.mp.setattr(new, '_load_release_validation', validator)
        self.mp.setattr(new, 'SYSTEMD_UNIT_DIR', self.unit_dir)
        self.mp.setattr(new, 'ensure_uv', lambda *a: self.events.append('prepare-deps'))
        self.mp.setattr(new.Updater, '_sync_deps', lambda _s: self.events.append('sync-deps'))
        def systemctl(worker, *args, **kw):
            self.events.append(args[0])
            if args[0] == 'stop' and self.fail == 'stop':
                raise new.UpdateError('injected stop failure')
        self.mp.setattr(new.Updater, '_systemctl', systemctl)
        original_swap = new.Updater._swap
        def swap(worker, staged):
            self.events.append('swap')
            if self.fail == 'swap':
                new._replace_dir(staged / 'app', worker.install_root / 'app')
                raise new.UpdateError('injected partial swap failure')
            original_swap(worker, staged)
        self.mp.setattr(new.Updater, '_swap', swap)
        def probe(_url, version, **kwargs):
            self.events.append('health:' + str(version))
            if version == '0.16.1' and self.fail == 'health':
                raise new.UpdateError('injected health failure')
            return True, 'isolated healthy'
        self.mp.setattr(new, '_probe_deployment', probe)
        # All following execution is the new actual apply path, including second
        # ZIP digest verification, config re-observation, claim, file swap and rollback.
        return new, new.main(handoff.args[2:])


@pytest.mark.parametrize('failure', ['', 'backup', 'stop', 'swap', 'health'])
async def test_actual_0160_first_hop_and_custom_config(tmp_path, monkeypatch, failure):
    env = Upgrade(tmp_path, monkeypatch)
    original = env.config.read_bytes()
    command = await env.launch()
    handoff = env.enter_old(command)
    env.fail = failure
    new, code = env.enter_new(handoff)
    result = json.loads((env.data / 'update-result.json').read_text())
    assert result['status'] == ('success' if not failure else 'rolled_back' if failure in {'swap', 'health'} else 'failed')
    assert code == (0 if not failure else 1)
    assert env.events.index('handoff') < env.events.index('stop')
    if failure != 'stop':
        assert env.events.index('stop') < env.events.index('backup-db')
    if failure not in {'backup', 'stop'}:
        assert env.events.index('backup-db') < env.events.index('swap')
        unit = (env.unit_dir / 'openbear.service').read_text()
        assert 'OPENBEAR_CONFIG=' + str(env.config).replace('%', '%%') in unit
        assert str(env.root / 'openbear.json') not in unit
    assert env.config.read_bytes() == original and env.config.stat().st_mode & 0o777 == 0o600
    assert env.database.read_text() == 'database boundary marker, not an SQLite DB'
    assert (env.root / 'skills/local/SKILL.md').read_text() == 'keep skill'
    assert (env.root / 'workspace/keep').read_text() == 'keep workspace'
    assert len([e for e in env.events if e == 'handoff']) == 1
    before = env.events.count('stop')
    assert new.main(handoff.args[2:]) == 1  # consumed handoff cannot apply twice
    assert env.events.count('stop') == before
    if failure:
        assert (env.root / 'app/main.py').read_text() == 'OLD = True\n'


async def test_0160_failed_backup_can_retry_with_new_request(tmp_path, monkeypatch):
    env = Upgrade(tmp_path, monkeypatch)
    command = await env.launch()
    handoff = env.enter_old(command)
    env.fail = 'backup'
    _, code = env.enter_new(handoff)
    assert code == 1
    env.fail = ''
    command = await env.launch()  # same old app, a fresh request ID as in the UI
    next_handoff = env.enter_old(command)
    assert next_handoff.args[-1] != handoff.args[-1]
    _, code = env.enter_new(next_handoff)
    assert code == 0


@pytest.mark.parametrize('damage', ['request', 'zip', 'helper'])
async def test_handoff_binds_request_zip_and_executing_code(tmp_path, monkeypatch, damage):
    env = Upgrade(tmp_path, monkeypatch)
    handoff = env.enter_old(await env.launch())
    directory = Path(handoff.args[1]).parent
    if damage == 'request':
        changed = dict(env.request, installRoot=str(tmp_path / 'another-install'))
        env.request_path.write_text(json.dumps(changed))
    elif damage == 'zip':
        with (directory / 'package.zip').open('ab') as f:
            f.write(b'tampered')
    else:
        with (directory / 'release_validation.py').open('a') as f:
            f.write('\n# tampered\n')
    _, code = env.enter_new(handoff)
    assert code == 1 and 'stop' not in env.events
    assert (env.root / 'app/main.py').read_text() == 'OLD = True\n'


async def test_legacy_unknown_runtime_fails_without_old_result_crash(tmp_path, monkeypatch):
    env = Upgrade(tmp_path, monkeypatch)
    command = await env.launch()
    (env.proc / '123/cmdline').write_bytes(b'python\0other.py\0')
    with pytest.raises(SystemExit) as stopped:
        env.enter_old(command)
    assert stopped.value.code == 1 and 'stop' not in env.events
    result = json.loads((env.data / 'update-result.json').read_text())
    assert result['status'] == 'failed' and '启动入口' in result['message']


async def test_legacy_resource_failure_is_safe_and_retryable(tmp_path, monkeypatch):
    env = Upgrade(tmp_path, monkeypatch)
    target = env.incoming / 'prompts/personas/little-code.tpl'
    original = target.read_bytes()
    target.unlink()
    env.digest = zip_tree(env.incoming, env.archive)
    command = await env.launch()
    with pytest.raises(SystemExit):
        env.enter_old(command)
    assert 'stop' not in env.events and 'handoff' not in env.events
    result = json.loads((env.data / 'update-result.json').read_text())
    assert result['status'] == 'failed' and '人设资源' in result['message']
    target.write_bytes(original)
    env.digest = zip_tree(env.incoming, env.archive)
    handoff = env.enter_old(await env.launch())
    _, code = env.enter_new(handoff)
    assert code == 0


async def test_new_launcher_uses_running_python_not_system_python(tmp_path, monkeypatch):
    from app.update import service
    calls = []
    async def create(*args, **kwargs):
        calls.append(args)
        async def communicate():
            return b'', b''
        return types.SimpleNamespace(returncode=0, communicate=communicate)
    monkeypatch.setattr(service.asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(service.shutil, 'which', lambda _n: '/usr/bin/python3.8')
    monkeypatch.setattr(service, 'sys_executable', lambda: str(tmp_path / '.venv/bin/python'))
    instance = object.__new__(service.UpdateService)
    instance.request_path = tmp_path / 'request.json'
    await instance._launch_updater(tmp_path / 'updater.py')
    assert calls[0][5] == str(tmp_path / '.venv/bin/python')


def test_plain_validation_never_handoffs_and_legacy_package_still_validates(tmp_path, monkeypatch):
    module = load(ROOT / 'scripts/release_validation.py', 'plain_validator')
    monkeypatch.setattr(module.os, 'execv', lambda *args: pytest.fail('ordinary validation must not execute upgrades'))
    release = make_release(tmp_path / 'release', '0.16.0')
    shutil.rmtree(release / 'prompts')
    (release / 'prompts').mkdir()
    assert module.validate_release_tree(release, '0.16.0')['ok']
    current = make_release(tmp_path / 'current', '0.16.1')
    assert module.validate_release_tree(current, '0.16.1')['ok']
    shutil.rmtree(current / 'prompts/personas')
    with pytest.raises(module.ReleaseValidationError, match='personas/manifest'):
        module.validate_release_tree(current, '0.16.1')


def test_installer_custom_config_reaches_browser_port_backup_and_success_unit(tmp_path):
    root, package = tmp_path / 'install', make_release(tmp_path / 'package', '0.16.1')
    config = write(tmp_path / 'settings with spaces/live-config.json', json.dumps({
        'storage': {'dbPath': str(tmp_path / 'custom-data/named.sqlite')}, 'web': {'port': 23456}}))
    config.chmod(0o600)
    original = config.read_bytes()
    write(root / 'openbear.json', '{"storage":{"dbPath":"data/wrong.db"},"web":{"port":19999}}')
    write(root / 'app/marker', 'old')
    write(root / '.venv/bin/python', '#!/bin/sh\nexit 0\n').chmod(0o755)
    events = tmp_path / 'events'
    fake_bin, units = tmp_path / 'bin', tmp_path / 'units'
    fake_bin.mkdir(); units.mkdir()
    # Only external service/dependency/SQLite boundaries are replaced.
    show = 'MainPID=0\nLoadState=loaded\nWorkingDirectory=' + str(root) + '\nEnvironment="OPENBEAR_CONFIG=' + str(config) + '" HOME=/root\n'
    fake_systemctl = write(fake_bin / 'systemctl', '#!/bin/sh\nif [ "$1" = show ]; then\ncat <<\'UNIT\'\n' + show + 'UNIT\nelse\nprintf "%s\\n" "$1" >> "' + str(events) + '"\nfi\n')
    fake_systemctl.chmod(0o755)
    backup = write(tmp_path / 'backup-boundary.py', f'''import json,sys
from pathlib import Path
args=sys.argv[1:]
assert args[1] == 'backup-database'
actual=Path(args[args.index('--config')+1])
assert actual == Path({str(config)!r})
db=json.loads(actual.read_text())['storage']['dbPath']
assert args[args.index('--expected-database')+1] == db
assert Path({str(events)!r}).read_text().splitlines()[-1] == 'stop'
with Path({str(events)!r}).open('a') as f: f.write('backup:'+db+'\\n')
p=Path(args[args.index('--backup-dir')+1])/'isolated-snapshot.marker'
p.parent.mkdir(parents=True,exist_ok=True); p.write_text('not SQLite')
print(json.dumps({{'created':True,'backupPath':str(p),'databasePath':db}}))
''')
    wrapper = write(tmp_path / 'python-wrapper', f'''#!/bin/bash
if [[ "${{1:-}}" == -c && "${{2:-}}" == "import aiogram, aiohttp, aiosqlite, httpx, pydantic, yaml" ]]; then
  echo deps >> "{events}"; exit 0
fi
if [[ "${{2:-}}" == backup-database ]]; then
  exec "{sys.executable}" "{backup}" "$@"
fi
exec "{sys.executable}" "$@"
''')
    wrapper.chmod(0o755)
    uv = write(fake_bin / 'uv', f'#!/bin/sh\nmkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\ncp "{wrapper}" "$UV_PROJECT_ENVIRONMENT/bin/python"\n')
    uv.chmod(0o755)
    write(package / 'scripts/install_browser_worker.py', f'''import sys
from pathlib import Path
assert Path(sys.argv[sys.argv.index('--config')+1]) == Path({str(config)!r})
with Path({str(events)!r}).open('a') as f: f.write('browser-config\\n')
''')
    library = install_library(tmp_path)
    harness = write(tmp_path / 'install-probe.sh', f'''#!/bin/bash
set -euo pipefail
source "{library}"
INSTALL_DIR="{root}"
SYSTEMD_UNIT_DIR="{units}"
RELEASE_VERSION=0.16.1
MODE=upgrade
export PATH="{fake_bin}:$PATH"
unset OPENBEAR_CONFIG
verify_web_ready() {{ echo verify >> "{events}"; }}
validate_release_package "{package}"
prepare_release_upgrade "{package}"
port="$(read_upgrade_web_port "{ROOT}" "{sys.executable}")"
[[ "$port" == 23456 ]]
commit_release_upgrade "$RELEASE_TXN"
''')
    proc = subprocess.run(['bash', str(harness)], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = events.read_text().splitlines()
    assert lines.index('deps') < lines.index('browser-config') < lines.index('stop')
    assert lines.index('stop') < lines.index('backup:' + str(tmp_path / 'custom-data/named.sqlite')) < lines.index('start') < lines.index('verify')
    unit = (units / 'openbear.service').read_text()
    assert 'OPENBEAR_CONFIG=' + str(config).replace('%', '%%') in unit
    assert config.read_bytes() == original and config.stat().st_mode & 0o777 == 0o600
    assert 'wrong.db' in (root / 'openbear.json').read_text()


def test_python38_bootstrap_syntax_and_unit_escaping():
    import ast
    # Grammar check is not a real 3.8 execution; the runtime regression above uses
    # the current interpreter and verifies the actual exec target explicitly.
    ast.parse((ROOT / 'scripts/release_validation.py').read_text(), feature_version=(3, 8))
    module = load(ROOT / 'scripts/release_validation.py', 'unit_validator')
    text = module.render_service_unit('[Service]\nEnvironment=OPENBEAR_CONFIG=__OPENBEAR_DIR__/openbear.json\n',
                                      Path('/opt/openbear'), '/private/"a% b".json', '/custom home')
    assert 'Environment="OPENBEAR_CONFIG=/private/\\"a%% b\\".json"' in text
    assert 'Environment="HOME=/custom home"' in text
    with pytest.raises(module.ReleaseValidationError):
        module.render_service_unit('[Service]\n', Path('/opt/openbear'), '/bad\nInjected=1')
