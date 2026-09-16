// MP-100 rev B; mm; geometry demo only
$fn=64;
difference() {
  cube([100,60,6]);
  for (x=[12,88], y=[12,48]) translate([x,y,-1]) cylinder(d=6.6,h=8);
}
