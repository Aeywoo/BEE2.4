"""Generate random quarter tiles, like in Destroyed or Retro maps."""
from collections import defaultdict

import random

from utils import Vec
from instanceLocs import resolve as resolve_inst

import utils
import vmfLib as VLib
import conditions
import vbsp

TEX_DEFAULT = [
    ('squarebeams', 'anim_wp/framework/squarebeams'),
    ('ceilingwalls', 'anim_wp/framework/backpanels_cheap'),
    ('floorbase', 'anim_wp/framework/backpanels'),
    ('tile_glue', 'concrete/concrete_modular_floor001e'),
    ('clip', 'tools/toolsplayerclip'),
]

# We want to force tiles with these overlay materials to appear!
FORCE_TILE_MATS = {
    mat
    for mat, key in
    vbsp.TEX_VALVE.items()
    if key.startswith('overlay.')
}

FORCE_LOCATIONS = set()


@conditions.meta_cond(priority=-1000, only_once=False)
def find_indicator_panels(inst):
    """We need to locate indicator panels, so they aren't overwritten.
    """
    if inst['file'].casefold() not in resolve_inst('[indpan]'):
        return
    loc = Vec(0, 0, -64).rotate_by_str(inst['angles'])
    loc += Vec.from_str(inst['origin'])

    # Sometimes (light bridges etc) a sign will be halfway between
    # tiles, so in that case we need to force 2 tiles.
    loc_min = (loc - (15, 15, 0)) // 32 * 32 + (16, 16, 0)
    loc_max = (loc + (15, 15, 0)) // 32 * 32 + (16, 16, 0)
    FORCE_LOCATIONS.add(loc_min.as_tuple())
    FORCE_LOCATIONS.add(loc_max.as_tuple())


@conditions.make_result('CutOutTile')
def res_cutout_tile(inst, res):
    """Generate random quarter tiles, like in Destroyed or Retro maps.

    - "MarkerItem" is the instance to look for.
    - "TileSize" can be "2x2" or "4x4".

    Materials:
    - "squarebeams" is the squarebeams variant to use.
    - "ceilingwalls" are the sides of the ceiling section.
    - "floorbase" is the texture under floor sections.
    - "tile_glue" is used on top of a thinner tile segment.
    - "clip" is the player_clip texture used over floor segments.
        (This allows customising the surfaceprop.)
    - "Floor4x4Black", "Ceil2x2White" and other combinations can be used to
       override the textures used.
    """
    item = resolve_inst(res['markeritem'])

    INST_LOCS = {}  # Map targetnames -> surface loc
    CEIL_IO = []  # Pairs of ceil inst corners to cut out.
    FLOOR_IO = []  # Pairs of floor inst corners to cut out.

    overlay_ids = {}  # When we replace brushes, we need to fix any overlays
    # on that surface.

    # The brushes which we're removing from the map. By leaving them
    # in the map until after the condition is evaulated, floor sections will
    # harmlessly modify each other's side textures instead of generating
    # brushes at the junction points of seperate tile 'groups'.
    brushes_to_remove = set()

    floor_edges = []  # Values to pass to add_floor_sides() at the end

    sign_loc = set(FORCE_LOCATIONS)
    # If any signage is present in the map, we need to force tiles to
    # appear at that location!
    for over in conditions.VMF.by_class['info_overlay']:
        if (
                over['material'].casefold() in FORCE_TILE_MATS and
                # Only check floor/ceiling overlays
                over['basisnormal'] in ('0 0 1', '0 0 -1')
                ):
            loc = Vec.from_str(over['origin'])
            # Sometimes (light bridges etc) a sign will be halfway between
            # tiles, so in that case we need to force 2 tiles.
            loc_min = (loc - (15, 15, 0)) // 32 * 32 + (16, 16, 0)
            loc_max = (loc + (15, 15, 0)) // 32 * 32 + (16, 16, 0)
            sign_loc.add(loc_min.as_tuple())
            sign_loc.add(loc_max.as_tuple())

    MATS = defaultdict(list)
    SETTINGS = {
        'floor_chance': utils.conv_int(
            res['floorChance', '100'], 100),
        'ceil_chance': utils.conv_int(
            res['ceilingChance', '100'], 100),
        'floor_glue_chance': utils.conv_int(
            res['floorGlueChance', '0']),
        'ceil_glue_chance': utils.conv_int(
            res['ceilingGlueChance', '0']),

        'beam_skin': res['squarebeamsSkin', '0'],

        'floor_edge': res['floorEdgeInst', ''],

        'quad_floor': res['FloorSize', '4x4'].casefold() == '2x2',
        'quad_ceil': res['CeilingSize', '4x4'].casefold() == '2x2',
    }

    for mat_prop in res['Materials', []]:
        MATS[mat_prop.name].append(mat_prop.value)

    for key, default in TEX_DEFAULT:
        if key not in MATS:
            MATS[key] = [default]

    # Find our marker ents
    for inst in conditions.VMF.by_class['func_instance']:
        if inst['file'].casefold() not in item:
            continue
        targ = inst['targetname']
        orient = Vec(0, 0, 1).rotate_by_str(inst['angles', '0 0 0'])
        # Check the orientation of the marker to figure out what to generate
        if orient == (0, 0, 1):
            io_list = FLOOR_IO
        else:
            io_list = CEIL_IO

        # Reuse orient to calculate where the solid face will be.
        loc = (orient * -64) + Vec.from_str(inst['origin'])
        INST_LOCS[targ] = loc

        outputs = {out.target for out in inst.outputs}
        for out in outputs:
            io_list.append((targ, out))
        if not outputs and inst.fixup['$connectioncount'] == '0':
            # If the item doesn't have any connections, 'connect'
            # it to itself so we'll generate a 128x128 tile segment.
            io_list.append((targ, targ))
        inst.remove()  # Remove the instance itself from the map.
    for start_floor, end_floor in FLOOR_IO:
        if end_floor not in INST_LOCS:
            # Not a marker!
            for inst in conditions.VMF.by_target[end_floor]:
                inst.remove()
            continue

        detail_ent = conditions.VMF.create_ent(
            classname='func_detail'
        )

        box_min = Vec(INST_LOCS[start_floor])
        box_min.min(INST_LOCS[end_floor])

        box_max = Vec(INST_LOCS[start_floor])
        box_max.max(INST_LOCS[end_floor])

        if box_min.z != box_max.z:
            continue  # They're not in the same level!
        z = box_min.z

        # Make the squarebeams props, using big models if possible
        gen_squarebeams(
            box_min + (-64, -64, 0),
            box_max + (64, 64, -8),
            skin=SETTINGS['beam_skin']
        )

        # Add a player_clip brush across the whole area
        conditions.VMF.add_brush(conditions.VMF.make_prism(
            p1=box_min - (64, 64, 8),
            p2=box_max + (64, 64, 0),
            mat=MATS['clip'][0],
        ).solid)

        # Add a noportal_volume covering the surface, in case there's
        # room for a portal.
        noportal_solid = conditions.VMF.make_prism(
            # Don't go all the way to the sides, so it doesn't affect wall
            # brushes.
            p1=box_min - (63, 63, 9),
            p2=box_max + (63, 63, 0),
            mat='tools/toolsinvisible',
        ).solid
        noportal_ent = conditions.VMF.create_ent(
            classname='func_noportal_volume',
            origin=box_min.join(' '),
        )
        noportal_ent.solids.append(noportal_solid)

        for x, y in utils.iter_grid(
                min_x=int(box_min.x),
                max_x=int(box_max.x)+1,
                min_y=int(box_min.y),
                max_y=int(box_max.y)+1,
                stride=128,
                ):
            convert_floor(
                Vec(x, y, z),
                overlay_ids,
                MATS,
                SETTINGS,
                sign_loc,
                detail_ent,
                brushes_to_remove,
            )

        # Mark borders we need to fill in, and the angle (for func_instance)
        for x in range(int(box_min.x), int(box_max.x)+1, 128):
            # North, South
            floor_edges.append((Vec(x, box_max.y + 64, z-64), '0 270 0'))
            floor_edges.append((Vec(x, box_min.y - 64, z-64), '0 90 0'))

        for y in range(int(box_min.y), int(box_max.y)+1, 128):
            # East, West
            floor_edges.append((Vec(box_max.x + 64, y, z-64), '0 180 0'))
            floor_edges.append((Vec(box_min.x - 64, y, z-64), '0 0 0'))

    add_floor_sides(floor_edges, MATS['squarebeams'], SETTINGS['floor_edge'])

    reallocate_overlays(overlay_ids)

    for loc, brush in brushes_to_remove:
        del conditions.SOLIDS[loc]
        conditions.VMF.remove_brush(brush)
    return True


def convert_floor(
        loc,
        overlay_ids,
        mats,
        settings,
        signage_loc,
        detail,
        brushes_to_remove,
):
    """Cut out tiles at the specified location."""
    try:
        brush = conditions.SOLIDS[loc.as_tuple()]
    except KeyError:
        return False  # No tile here!

    # The new brush IDs overlays need to use
    # NOTE: strings, not ints!
    ant_locs = overlay_ids[str(brush.face.id)] = []

    # Defer removing brushes until later -
    # otherwise adding border textures doesn't work correctly
    brushes_to_remove.add((loc.as_tuple(), brush.solid))

    loc.x -= 64
    loc.y -= 64

    random.seed('cutout_tile' + loc.join(' '))
    tile_map = [
        (random.randint(0, 100) < settings['floor_chance'])
        for _ in range(16)
    ]

    for x, y in utils.iter_grid(max_x=4, max_y=4):
        tile_loc = loc + (x*32 + 16, y*32 + 16, 0)
        if tile_loc.as_tuple() in signage_loc:
            should_make_tile = True
            # We don't need to check this again in future!
            signage_loc.remove(tile_loc.as_tuple())
        else:
            should_make_tile = tile_map[x*4 + y]
        if should_make_tile:
            # Full tile
            tile = make_tile(
                p1=tile_loc - (16, 16, 0),
                p2=tile_loc + (16, 16, -2),
                top_mat=vbsp.get_tex(str(brush.color) + '.floor'),
                bottom_mat='tools/toolsnodraw',
                beam_mat=random.choice(mats['squarebeams']),
            )
            detail.solids.append(tile.solid)
            ant_locs.append(str(tile.top.id))
        elif random.randint(0, 100) < settings['floor_glue_chance']:
            # 'Glue' tile
            tile = make_tile(
                p1=tile_loc - (16, 16, 1),
                p2=tile_loc + (16, 16, -2),
                top_mat=random.choice(mats['tile_glue']),
                bottom_mat='tools/toolsnodraw',
                beam_mat=random.choice(mats['squarebeams']),
            )
            detail.solids.append(tile.solid)
        else:
            # No tile at this loc!
            pass

    base_brush = conditions.VMF.make_prism(
        p1=loc + (0, 0, -9),
        p2=loc + (128, 128, -8)
    )
    base_brush.top.mat = random.choice(mats['floorbase'])
    conditions.VMF.add_brush(base_brush.solid)

    return True


def make_tile(p1, p2, top_mat, bottom_mat, beam_mat):
    """Generate a 2 or 1 unit thick squarebeams tile.

    """
    prism = conditions.VMF.make_prism(p1, p2)
    brush, t, b, n, s, e, w = prism
    t.mat = top_mat
    b.mat = bottom_mat

    n.mat = beam_mat
    s.mat = beam_mat
    e.mat = beam_mat
    w.mat = beam_mat

    thickness = abs(p1.z - p2.z)

    if thickness == 2:
        # The z-axis texture offset needed
        # The texture is 512 high, so wrap around
        # 56 is the offset for the thin-line part of squarebeams
        # Textures are at 0.25 size, so 4 per hammer unit
        z_off = ((max(p1.z, p2.z) * 4) + 56) % 512
    elif thickness == 1:
        # Slightly different offset, so the line is still centered
        z_off = ((max(p1.z, p2.z) * 4) + 54) % 512
    else:
        raise ValueError(
            'Tile has incorrect thickness '
            '(expected 1 or 2, got {})'.format(thickness)
        )

    n.uaxis = VLib.UVAxis(
        0, 0, 1, offset=z_off)
    n.vaxis = VLib.UVAxis(
        1, 0, 0, offset=0)
    s.uaxis = n.uaxis.copy()
    s.vaxis = n.vaxis.copy()

    e.uaxis = VLib.UVAxis(
        0, 0, 1, offset=z_off)
    e.vaxis = VLib.UVAxis(
        0, 1, 0, offset=0)
    w.uaxis = e.uaxis.copy()
    w.vaxis = e.vaxis.copy()

    # Ensure the squarebeams textures aren't replaced, as well as floor tex
    vbsp.IGNORED_FACES.update(brush.sides)

    return prism


def _make_squarebeam(x, y, z, skin='0', size=''):
    """Make a squarebeam prop at the given location."""
    conditions.VMF.create_ent(
        classname='prop_static',
        angles='0 0 0',
        origin='{} {} {}'.format(x, y, z),
        model='models/anim_wp/framework/squarebeam_off' + size + '.mdl',
        skin=skin,
        disableshadows='1',
    )


def gen_squarebeams(p1, p2, skin, gen_collision=True):
    """Generate squarebeams props to fill the space given.

    The space should be in multiples of 64. The squarebeams brush will
    be aligned to the lowest point in the space.
    """
    z = min(p1.z, p2.z) + 8
    min_x = min(p1.x, p2.x)
    min_y = min(p1.y, p2.y)
    max_x = max(p1.x, p2.x)
    max_y = max(p1.y, p2.y)

    dist_x = max_x - min_x
    dist_y = max_y - min_y

    # After this x or y dist, move to the next grid size.
    cutoff_512_x = dist_x // 512 * 512
    cutoff_256_x = dist_x // 256 * 256
    cutoff_128_x = dist_x // 128 * 128

    cutoff_512_y = dist_y // 512 * 512
    cutoff_256_y = dist_y // 256 * 256
    cutoff_128_y = dist_y // 128 * 128

    for x, y in utils.iter_grid(
            max_x=int(dist_x),
            max_y=int(dist_y),
            stride=64,
            ):
        if x < cutoff_512_x and y < cutoff_512_y:
            # Make 1 prop every 512 units, at the center
            if x % 512 == 0 and y % 512 == 0:
                _make_squarebeam(
                    min_x+x+256, min_y+y+256, z,
                    skin, '_8x8',
                )
        elif x < cutoff_256_x and y < cutoff_256_y:
            if x % 256 == 0 and y % 256 == 0:
                _make_squarebeam(
                    min_x+x+128, min_y+y+128, z,
                    skin, '_4x4',
                )
        elif x < cutoff_128_x and y < cutoff_128_y:
            if x % 128 == 0 and y % 128 == 0:
                _make_squarebeam(
                    min_x+x+64, min_y+y+64, z,
                    skin, '_2x2',
                )
        else:
            # Make squarebeams for every point!
            _make_squarebeam(
                min_x + x + 32, min_y+y + 32, z,
                skin,
            )

    if gen_collision:
        collision = conditions.VMF.create_ent(
            classname='func_brush',
            disableshadows='1',
            disableflashlight='1',
            disablereceiveshadows='1',
            shadowdepthnocache='1',
            solidity='2',  # Always Solid
            solidbsp='1',
        )
        for x in range(int(min_x)+64, int(max_x), 64):
            collision.solids.append(
                conditions.VMF.make_prism(
                    p1=Vec(x-2, min_y+2, z-2),
                    p2=Vec(x+2, max_y-2, z-8),
                    mat='tools/toolsnodraw',
                ).solid
            )
        for y in range(int(min_y)+64, int(max_y), 64):
            collision.solids.append(
                conditions.VMF.make_prism(
                    p1=Vec(min_x+2, y-2, z-2),
                    p2=Vec(max_x-2, y+2, z-8),
                    mat='tools/toolsnodraw',
                ).solid
            )
        for x1, y1, x2, y2 in [
                (min_x, min_y, max_x, min_y+2),
                (min_x, max_y, max_x, max_y-2),
                (min_x, min_y, min_x+2, max_y),
                (max_x, min_y, max_x-2, max_y),
                ]:
            collision.solids.append(
                conditions.VMF.make_prism(
                    p1=Vec(x1, y1, z-2),
                    p2=Vec(x2, y2, z-8),
                    mat='tools/toolsnodraw',
                ).solid
            )


def reallocate_overlays(mapping):
    """Fix any overlay faces which were removed.
    This makes antlines continue to appear on the small tiles.
    """
    for overlay in conditions.VMF.by_class['info_overlay']:
        sides = overlay['sides', ''].split(' ')
        for side in sides[:]:
            if side not in mapping:
                continue
            sides.remove(side)
            sides.extend(mapping[side])
        if not sides:
            # The overlay doesn't have any sides at all!
            conditions.VMF.remove_ent(overlay)
        else:
            overlay['sides'] = ' '.join(sides)


def add_floor_sides(locs, tex, file):
    """We need to replace nodraw textures around the outside of the holes.

    This requires looping through all faces, since these will have been
    nodrawed.
    """
    added_locations = {
        loc.as_tuple(): False
        for loc, _ in
        locs
    }

    for face in conditions.VMF.iter_wfaces(world=True, detail=False):
        if face.mat != 'tools/toolsnodraw':
            continue
        loc = face.get_origin().as_tuple()
        if loc in added_locations:
            random.seed('floor_side_{}_{}_{}'.format(*loc))
            face.mat = random.choice(tex)
            added_locations[loc] = True
            # Swap these to flip the texture diagonally, so the beam is at top
            face.uaxis, face.vaxis = face.vaxis, face.uaxis
            face.uaxis.offset = 48

            vbsp.IGNORED_FACES.add(face)

    # Look for the ones without a texture - these are open to the void and
    # need to be sealed. We use an instance to allow chamfering the edges
    # to prevent showing void at outside corners.
    for loc, angles in locs:
        if added_locations[loc.as_tuple()]:
            continue

        conditions.VMF.create_ent(
            classname='func_instance',
            file=file,
            origin=loc.join(' '),
            angles=angles,
        )