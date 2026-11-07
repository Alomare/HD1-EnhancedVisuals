// Ambient occlusion, in place of the game's own vo_ssao pixel shader, which ships unused.
//
// Binds what vo_ssao binds, in the same slots, so the game's tables for that shader stay true:
// t0/s0 the linear depth (metres, the "depth" target), t1/s1 vo_rotation (4x4 unit vectors, RG16F),
// b0 global_camera (camera_near_far), b1 c0 (input_texture0_size, ssao_radius, max_distance).
// Writes what vo_ssao writes, so the game's ssao_blur and ssao_upscale read it as they were made to:
// x the light left after occlusion (1 = none), yz the depth over the far plane packed in two bytes, w 0.
//
// The method is a sampled hemisphere around a normal rebuilt from depth, 12 taps in a spiral turned
// per pixel by vo_rotation, with a fall-off at the 1.25 m radius. ssao_radius is the only live value this
// shader's tables offer, so it carries the strength instead: the occlusion is raised to ten times it
// (the mod sets 0.1, 0.2 or 0.3; the planets' leftovers are clamped). ssao_upscale's ssao_strength
// stays at 1 from the mod, and ao_apply.hlsl reads the same result.

cbuffer global_camera : register(b0)
{
	float3 camera_near_far;
};

cbuffer c0 : register(b1)
{
	float4x4 world_view_proj;
	float2 input_texture0_size;
	float ssao_radius;
	float max_distance;
};

Texture2D __tex_input_texture0 : register(t0);
SamplerState __samp_input_texture0 : register(s0);
Texture2D __tex_vo_rotation : register(t1);
SamplerState __samp_vo_rotation : register(s1);

struct PS_INPUT
{
	float4 position : SV_POSITION;
	float2 uv : TEXCOORD0;
};

// The game's camera: 60 degrees of vertical field of view (camera_manager.lua).
static const float TAN_HALF_FOV = 0.57735;
static const int TAPS = 12;
static const float INTENSITY = 6.0;

// A spiral of TAPS points, 7 turns, each at its share of the radius: (cos, sin) * t, t = (k + 0.5) / TAPS.
static const float2 SPIRAL[TAPS] = {
	float2(-0.0108, 0.0402), float2(0.0884, -0.0884), float2(-0.2012, 0.0539), float2(0.2817, 0.0755),
	float2(-0.2652, -0.2652), float2(0.1186, 0.4427), float2(0.1402, -0.5232), float2(-0.4419, 0.4419),
	float2(0.6842, -0.1833), float2(-0.7647, -0.2049), float2(0.6187, 0.6187), float2(-0.2480, -0.9257),
};

float depth_at(int2 p, int2 size)
{
	return __tex_input_texture0.Load(int3(clamp(p, int2(0, 0), size - 1), 0)).x;
}

float3 view_pos(float2 uv, float z, float aspect)
{
	float2 ndc = float2(uv.x * 2.0 - 1.0, 1.0 - uv.y * 2.0);
	return float3(ndc.x * TAN_HALF_FOV * aspect * z, ndc.y * TAN_HALF_FOV * z, z);
}

float2 pack_depth(float d)
{
	float2 e = frac(float2(1.0, 255.0) * d);
	return e - e.y / 255.0;
}

float4 ps_main(PS_INPUT input) : SV_TARGET
{
	uint w, h;
	__tex_input_texture0.GetDimensions(w, h);
	int2 size = int2(w, h);
	float2 fsize = float2(w, h);
	float aspect = fsize.x / fsize.y;
	float2 texel = 1.0 / fsize;

	int2 c = int2(input.uv * fsize);
	float2 uv = (float2(c) + 0.5) * texel;
	float z = depth_at(c, size);
	float far_plane = max(camera_near_far.y, 1.0);
	float2 packed = pack_depth(saturate(z / far_plane));
	if (z <= 0.0 || z >= far_plane * 0.95)
		return float4(1.0, packed, 0.0);

	float3 p = view_pos(uv, z, aspect);

	// The normal, from the nearer neighbour on each axis, so edges don't bend it.
	float zl = depth_at(c + int2(-1, 0), size), zr = depth_at(c + int2(1, 0), size);
	float zu = depth_at(c + int2(0, -1), size), zd = depth_at(c + int2(0, 1), size);
	float3 dx = abs(zr - z) < abs(z - zl)
		? view_pos(uv + float2(texel.x, 0), zr, aspect) - p
		: p - view_pos(uv - float2(texel.x, 0), zl, aspect);
	float3 dy = abs(zd - z) < abs(z - zu)
		? view_pos(uv + float2(0, texel.y), zd, aspect) - p
		: p - view_pos(uv - float2(0, texel.y), zu, aspect);
	float3 n = normalize(cross(dx, dy));
	if (dot(n, p) > 0.0)
		n = -n;

	float radius = 1.25;
	float r2 = radius * radius;
	// The radius on screen, capped so a surface close to the camera doesn't fetch across the picture.
	float screen_r = min(radius / (2.0 * z * TAN_HALF_FOV), 0.12);
	float2 reach = float2(screen_r / aspect, screen_r);

	float2 rot = __tex_vo_rotation.Load(int3(int2(input.position.xy) & 3, 0)).xy;
	float bias = 0.002 * z;
	float sum = 0.0;
	[unroll]
	for (int k = 0; k < TAPS; ++k)
	{
		float2 d = SPIRAL[k];
		d = float2(d.x * rot.x - d.y * rot.y, d.x * rot.y + d.y * rot.x);
		float2 suv = uv + d * reach;
		int2 sp = int2(suv * fsize);
		float sz = depth_at(sp, size);
		float3 q = view_pos((float2(sp) + 0.5) * texel, sz, aspect);
		float3 v = q - p;
		float vv = dot(v, v);
		float vn = dot(v, n);
		float f = max(r2 - vv, 0.0) / r2;
		sum += f * f * max((vn - bias) / sqrt(vv + 1e-4), 0.0);
	}
	float ao = pow(saturate(1.0 - INTENSITY * sum / TAPS), clamp(ssao_radius * 10.0, 0.3, 4.0));
	// Never true: keeps the samplers and max_distance in the shader's tables, as vo_ssao has them.
	if (max_distance < -1e30)
		ao += __tex_input_texture0.SampleLevel(__samp_input_texture0, uv, 0).x
			+ __tex_vo_rotation.SampleLevel(__samp_vo_rotation, uv, 0).x;
	return float4(ao, packed, 0.0);
}
