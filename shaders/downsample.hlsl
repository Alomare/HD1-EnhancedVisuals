// Render scale: the finished scene, drawn larger than the screen, brought down to the screen's size, in
// place of the game's copy:encode_as_rgbm pixel shader, which only the editor's cubemap capture runs (the
// headers match copy's apart from the name, so the render state is an opaque write).
//
// t0/s0 the scene at render scale (the picture before color grading). Each output pixel averages the
// source texels under its footprint, each weighted by how much of it the footprint covers: a box filter
// of exactly the scale, which at 2x is the plain 2x2 average and at 1.5x doesn't skip or double texels.
// The footprint comes from the derivatives of the texture coordinate, so the shader needs no constants.

Texture2D __tex_input_texture0 : register(t0);
SamplerState __samp_input_texture0 : register(s0);

struct PS_INPUT
{
	float4 position : SV_POSITION;
	float2 uv : TEXCOORD0;
};

float4 ps_main(PS_INPUT input) : SV_TARGET
{
	uint w, h;
	__tex_input_texture0.GetDimensions(w, h);
	float2 size = float2(w, h);
	float2 foot = max(float2(abs(ddx(input.uv.x)), abs(ddy(input.uv.y))) * size, 1.0);
	float2 lo = input.uv * size - foot * 0.5;
	float2 hi = lo + foot;
	int2 first = int2(floor(lo));
	float4 sum = 0.0;
	float total = 0.0;
	[unroll] for (int y = 0; y < 4; y++)
	{
		float wy = saturate(min(hi.y, first.y + y + 1.0) - max(lo.y, first.y + y));
		[unroll] for (int x = 0; x < 4; x++)
		{
			float wx = saturate(min(hi.x, first.x + x + 1.0) - max(lo.x, first.x + x));
			int2 p = clamp(first + int2(x, y), int2(0, 0), int2(w, h) - 1);
			sum += __tex_input_texture0.Load(int3(p, 0)) * (wx * wy);
			total += wx * wy;
		}
	}
	float4 outc = sum / max(total, 1e-6);
	// Never true: keeps the sampler in the shader's tables, as the shader it replaces has it.
	if (outc.a < -1.0)
		outc += __tex_input_texture0.SampleLevel(__samp_input_texture0, input.uv, 0);
	return outc;
}
