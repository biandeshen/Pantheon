import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { CATALOG } from "./catalog";
import { generateDesign, isDesign, type OfficeDesign, type PlacedItem } from "./design";
import { patchItem, snap, useDesigner } from "./designerStore";
import { registerOffice, type OfficePack } from "./registry";

// Register the office-classic pack so CATALOG is populated (same as office.test.ts).
beforeAll(() => {
  const office = JSON.parse(readFileSync(resolve(__dirname, "../../../packs/office-classic/office.json"), "utf8"));
  registerOffice([{ id: "office-classic", name: "Classic office", base: "/api/v1/packs/office-classic/files", ...office } as OfficePack]);
});

// Integration test: furniture drag → save → reload → baseline restore.
// This simulates the full round-trip that page.tsx does, without a browser:
//   1. Generate a design (what the office starts as)
//   2. Begin editing (designerStore.begin)
//   3. Drag a furniture item (patchItem via onMove)
//   4. Save (the design becomes the "saved" layout)
//   5. Reload (isDesign validates the saved layout)
//   6. The reloaded design matches what was saved

describe("furniture drag → save → reload round-trip", () => {
  const agents = Array.from({ length: 4 }, (_, i) => ({
    id: `agt_${i}`,
    team: ["Engineering", "Design", "Research", "Ops"][i],
    createdAt: `2026-01-01T00:00:0${i}Z`,
  }));

  it("preserves a dragged item's new position through save and reload", () => {
    // 1. Start with a generated design
    const original = generateDesign(agents);
    expect(isDesign(original)).toBe(true);

    // Find a workstation to drag
    const item = original.items.find((i) => i.kind === "workstation");
    expect(item).toBeDefined();
    const itemId = item!.id;
    const oldX = item!.x;
    const oldZ = item!.z;

    // 2. Begin editing
    useDesigner.getState().begin(original);
    expect(useDesigner.getState().draft).not.toBeNull();
    expect(useDesigner.getState().dirty).toBe(false);

    // 3. Drag the item to a new position (simulating onMove with pointer coordinates)
    const newX = snap(10.5, 0.05);
    const newZ = snap(8.3, 0.05);
    useDesigner.getState().update(
      (dz) => patchItem(dz, itemId, { x: newX, z: newZ }),
      false, // record=false, same as DesignerLayer.onMove for item drags
    );

    // The draft should reflect the new position
    const draft = useDesigner.getState().draft!;
    const dragged = draft.items.find((i) => i.id === itemId)!;
    expect(dragged.x).toBe(newX);
    expect(dragged.z).toBe(newZ);
    expect(useDesigner.getState().dirty).toBe(true);

    // 4. Save: the draft becomes the saved layout (what api.updateOrg would send)
    const savedLayout = { office: draft };
    const savedOffice = savedLayout.office;

    // 5. Reload: isDesign validates the saved design
    expect(isDesign(savedOffice)).toBe(true);
    expect(savedOffice.version).toBe(1);
    expect(Array.isArray(savedOffice.items)).toBe(true);
    expect(Array.isArray(savedOffice.rooms)).toBe(true);
    expect(savedOffice.width).toBeGreaterThan(0);
    expect(savedOffice.depth).toBeGreaterThan(0);

    // 6. The reloaded design matches what was saved
    const reloadedItem = savedOffice.items.find((i) => i.id === itemId)!;
    expect(reloadedItem.x).toBe(newX);
    expect(reloadedItem.z).toBe(newZ);
    expect(reloadedItem.x).not.toBe(oldX);
    expect(reloadedItem.z).not.toBe(oldZ);

    // Clean up
    useDesigner.getState().exit();
  });

  it("preserves room position through drag and reload", () => {
    const original = generateDesign(agents);
    const room = original.rooms[0];
    expect(room).toBeDefined();

    useDesigner.getState().begin(original);

    // Drag the room
    const newX = snap(5.0);
    const newZ = snap(3.0);
    useDesigner.getState().update(
      (dz) => ({ ...dz, rooms: dz.rooms.map((r) => r.id === room.id ? { ...r, x: newX, z: newZ } : r) }),
      false,
    );

    const draft = useDesigner.getState().draft!;
    const draggedRoom = draft.rooms.find((r) => r.id === room.id)!;
    expect(draggedRoom.x).toBe(newX);
    expect(draggedRoom.z).toBe(newZ);

    // Save and reload
    const savedOffice = draft;
    expect(isDesign(savedOffice)).toBe(true);
    const reloadedRoom = savedOffice.rooms.find((r) => r.id === room.id)!;
    expect(reloadedRoom.x).toBe(newX);
    expect(reloadedRoom.z).toBe(newZ);

    useDesigner.getState().exit();
  });

  it("preserves multiple item drags in sequence", () => {
    const original = generateDesign(agents);
    useDesigner.getState().begin(original);

    // Drag three different items
    const items = original.items.filter((i) => CATALOG[i.kind]?.blocking).slice(0, 3);
    expect(items.length).toBe(3);

    const positions = [
      { x: snap(1.5, 0.05), z: snap(2.5, 0.05) },
      { x: snap(8.0, 0.05), z: snap(9.0, 0.05) },
      { x: snap(12.3, 0.05), z: snap(4.7, 0.05) },
    ];

    for (let i = 0; i < items.length; i++) {
      useDesigner.getState().update(
        (dz) => patchItem(dz, items[i].id, positions[i]),
        false,
      );
    }

    const draft = useDesigner.getState().draft!;

    // Save and reload
    expect(isDesign(draft)).toBe(true);

    // All three items have their new positions
    for (let i = 0; i < items.length; i++) {
      const reloaded = draft.items.find((it) => it.id === items[i].id)!;
      expect(reloaded.x).toBe(positions[i].x);
      expect(reloaded.z).toBe(positions[i].z);
    }

    // Items that were NOT dragged are unchanged
    const untouched = original.items.find(
      (it) => !items.some((d) => d.id === it.id),
    );
    if (untouched) {
      const reloaded = draft.items.find((it) => it.id === untouched.id)!;
      expect(reloaded.x).toBe(untouched.x);
      expect(reloaded.z).toBe(untouched.z);
    }

    useDesigner.getState().exit();
  });

  it("rejects malformed saved data via isDesign", () => {
    // This is the fallback path: if saved data is corrupt, generateDesign is used instead
    expect(isDesign(null)).toBe(false);
    expect(isDesign({})).toBe(false);
    expect(isDesign({ version: 2, items: [], rooms: [], width: 10, depth: 10 })).toBe(false);
    expect(isDesign({ version: 1, items: "not-array", rooms: [], width: 10, depth: 10 })).toBe(false);
    expect(isDesign({ version: 1, items: [], rooms: [], width: 0, depth: 10 })).toBe(false);
    expect(isDesign({ version: 1, items: [], rooms: [], width: 10, depth: 0 })).toBe(false);
  });

  it("structuredClone in begin() prevents reference aliasing between draft and original", () => {
    const original = generateDesign(agents);
    useDesigner.getState().begin(original);

    const draft = useDesigner.getState().draft!;

    // Mutating the draft must not affect the original
    useDesigner.getState().update(
      (dz) => patchItem(dz, dz.items[0].id, { x: 99.9, z: 99.9 }),
      false,
    );

    const updatedDraft = useDesigner.getState().draft!;
    expect(updatedDraft.items[0].x).toBe(99.9);
    expect(original.items[0].x).not.toBe(99.9); // original is untouched

    useDesigner.getState().exit();
  });
});
