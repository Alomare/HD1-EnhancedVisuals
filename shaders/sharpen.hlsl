// Contrast-adaptive sharpening, in place of the game's filter:gaussian_blur_13tap pixel shader, which the
// render config never runs. Runs after FXAA, on the finished picture.
//
// Binds what that shader binds: t0/s0 the input, b0 c0 (world_view_proj, inv_input_texture0_size).
// The texel size comes from the texture itself, so nothing depends on the engine filling c0.
//
// Each pixel is pushed away from the average of its four neighbours, by an amount that shrinks where
// the neighbourhood already has strong contrast, so edges don't ring and dark or bright areas don't
// clip. SHARPNESS is set at build time, one shader per strength (tools/build.py).

#ifndef SHARPNESS
#define SHARPNESS 0.5
#endif

cbuffer c0 : register(b0)
{
	float4x4 world_view_proj;
	float2 inv_input_texture0_size;
};

Texture2D __tex_input_texture0 : register(t0);
SamplerState __samp_input_texture0 : register(s0);

struct PS_INPUT
{
	float4 position : SV_POSITION;
	float2 uv : TEXCOORD0;
};

float3 at(int2 p, int2 size)
{
	return __tex_input_texture0.Load(int3(clamp(p, int2(0, 0), size - 1), 0)).rgb;
}

float4 ps_main(PS_INPUT input) : SV_TARGET
{
	uint w, h;
	__tex_input_texture0.GetDimensions(w, h);
	int2 size = int2(w, h);
	int2 c = int2(input.uv * float2(w, h));

	float3 a = at(c + int2(-1, -1), size), b = at(c + int2(0, -1), size), cc = at(c + int2(1, -1), size);
	float3 d = at(c + int2(-1, 0), size), e = at(c, size), f = at(c + int2(1, 0), size);
	float3 g = at(c + int2(-1, 1), size), hh = at(c + int2(0, 1), size), i = at(c + int2(1, 1), size);

	// The darkest and brightest of the cross, then of the whole 3x3, added: a soft minimum and maximum.
	float3 lo = min(min(min(d, e), min(f, b)), hh);
	lo += min(lo, min(min(a, cc), min(g, i)));
	float3 hi = max(max(max(d, e), max(f, b)), hh);
	hi += max(hi, max(max(a, cc), max(g, i)));

	// Room to sharpen before clipping, as a share of the brightest: small at strong edges.
	float3 amp = saturate(min(lo, 2.0 - hi) / max(hi, 1e-4));
	amp = sqrt(amp);
	float peak = -1.0 / lerp(8.0, 5.0, saturate(SHARPNESS));
	float3 wgt = amp * peak;
	float3 outc = ((b + d + f + hh) * wgt + e) / (1.0 + 4.0 * wgt);
	// Never true: keeps the sampler and c0 in the shader's tables, as the shader it replaces has them.
	if (inv_input_texture0_size.x < 0.0)
		outc += __tex_input_texture0.SampleLevel(__samp_input_texture0, input.uv, 0).rgb;
	return float4(saturate(outc), 1.0);
}
